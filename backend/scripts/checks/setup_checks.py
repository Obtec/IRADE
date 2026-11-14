"""Fresh-checkout TLS setup regression tests; no Docker daemon or running DB."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='irade-manual-tls-')
        cls.bundle = Path(cls.fixture.name) / 'certificates'
        cls.bundle.mkdir()
        cls.openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '365',
                    '-subj', '/CN=Setup test CA', '-keyout', str(Path(cls.fixture.name) / 'ca.key'),
                    '-out', str(cls.bundle / 'root-ca.pem'))
        sql_ca = Path(cls.fixture.name) / 'sql-ca.crt'
        sql_ca_key = Path(cls.fixture.name) / 'sql-ca.key'
        cls.openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '365',
                    '-subj', '/CN=Separate SQL test CA', '-keyout', str(sql_ca_key), '-out', str(sql_ca))
        leaves = {
            '': ('fullchain.pem', 'privkey.pem', ['DNS:localhost', 'IP:127.0.0.1', 'DNS:irade-nginx']),
            'internal/spring': ('fullchain.pem', 'privkey.pem', ['DNS:localhost', 'DNS:irade-spring']),
            'internal/cpp': ('fullchain.pem', 'privkey.pem', ['DNS:localhost', 'DNS:irade-cpp']),
            'internal/frontend': ('fullchain.pem', 'privkey.pem', ['DNS:localhost', 'DNS:irade-frontend-dev']),
            'internal/redis-insight': ('fullchain.pem', 'privkey.pem', ['DNS:localhost', 'DNS:redis-insight', 'IP:127.0.0.1']),
            'mssql': ('server.crt', 'server.key', ['DNS:localhost', 'DNS:irade-mssql', 'DNS:mssql', 'IP:127.0.0.1']),
            'redis': ('server.crt', 'server.key', [f'IP:10.26.0.{n}' for n in (2, 7, 5, 6, 3, 4)]),
            'elasticsearch': ('http.crt', 'http.key', ['DNS:localhost', 'DNS:irade-elasticsearch', 'IP:127.0.0.1']),
        }
        for number, (directory, (cert, key, sans)) in enumerate(leaves.items(), 1):
            target = cls.bundle / directory
            target.mkdir(parents=True, exist_ok=True)
            request = Path(cls.fixture.name) / f'{number}.csr'
            extensions = Path(cls.fixture.name) / f'{number}.cnf'
            extensions.write_text('basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=' + ','.join(sans) + '\n')
            cls.openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=Setup test leaf',
                        '-keyout', str(target / key), '-out', str(request))
            issuing_ca = sql_ca if directory == 'mssql' else cls.bundle / 'root-ca.pem'
            issuing_key = sql_ca_key if directory == 'mssql' else Path(cls.fixture.name) / 'ca.key'
            cls.openssl('x509', '-req', '-in', str(request), '-CA', str(issuing_ca),
                        '-CAkey', str(issuing_key), '-set_serial', str(number),
                        '-days', '365', '-extfile', str(extensions), '-out', str(target / cert))
            if directory == 'mssql':
                converted = target / 'converted.key'
                cls.openssl('rsa', '-in', str(target / key), '-out', str(converted))
                if '-----BEGIN RSA PRIVATE KEY-----' not in converted.read_text():
                    cls.openssl('rsa', '-in', str(target / key), '-traditional', '-out', str(converted))
                converted.replace(target / key)
            if directory:
                shutil.copyfile(issuing_ca, target / ('ca.pem' if directory.startswith('internal/') else 'ca.crt'))
        with (cls.bundle / 'root-ca.pem').open('a') as stream:
            stream.write(sql_ca.read_text())
        # Runtime certificate input never contains the issuing private key.
        cls.assert_bundle = {str(path.relative_to(cls.bundle)): path.read_bytes() for path in cls.bundle.rglob('*') if path.is_file()}

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    @staticmethod
    def openssl(*args):
        subprocess.run(['openssl', *args], check=True, capture_output=True)

    def checkout(self, temporary):
        parent = Path(temporary)
        for repo, files in {
            'backend': ['.env.example', 'scripts/setup-docker.py', 'scripts/sync-docker-env.py', 'cpp/config/freetds-docker-tls.conf'],
            'db': ['ops/configure-db.py', 'mssql/.env.example', 'redis/.env.example', 'elasticsearch/.env.example'],
            'wall': ['.env.example', 'scripts/setup.py'],
        }.items():
            for relative in files:
                source = ROOT / relative if repo == 'backend' else ROOT.parent / repo / relative
                target = parent / repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        tls = parent / 'manual certificates'
        shutil.copytree(self.bundle, tls)
        return parent, tls

    def run_setup(self, parent, tls, *args, extra_env=None):
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **(extra_env or {})}
        be = subprocess.run([sys.executable, str(parent / 'backend/scripts/setup-docker.py'),
                             '--db-dir', str(parent / 'db'), '--tls-dir', str(tls), *args],
                            capture_output=True, text=True,
                            env=env)
        if be.returncode:
            return be
        return subprocess.run([sys.executable, str(parent / 'wall/scripts/setup.py'),
                               '--tls-dir', str(tls), '--backend-dir', str(parent / 'backend'), *args],
                              capture_output=True, text=True,
                              env=env)

    def test_fresh_clone_setup_is_verified_tls_and_repeatable(self):
        with tempfile.TemporaryDirectory(prefix='irade checkout ') as directory:
            parent, tls = self.checkout(directory)
            result = self.run_setup(parent, tls)
            self.assertEqual(result.returncode, 0, result.stderr)
            envs = sorted(parent.rglob('.env'))
            self.assertEqual(len(envs), 5)
            contents = {path: path.read_bytes() for path in envs}
            for path in envs:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                active = '\n'.join(line for line in path.read_text().splitlines() if not line.lstrip().startswith('#'))
                self.assertNotIn('change-me', active)
            be = (parent / 'backend/.env').read_text()
            self.assertIn('DB_ENCRYPT=true', be)
            self.assertIn('DB_TRUST_SERVER_CERTIFICATE=false', be)
            self.assertIn('REDIS_TLS_ENABLED=true', be)
            self.assertIn('ES_SCHEME=https', be)
            self.assertIn('REDIS_BROKER_ROUTES=', be)
            self.assertIn('NGINX_BACKEND_IP=10.29.0.250', be)
            wall = (parent / 'wall/.env').read_text()
            self.assertIn('STORAGE_REDIS_REPLICA_1_UPSTREAM=', wall)
            self.assertIn('10.26.0.7:6379', wall)
            db = (parent / 'db/mssql/.env').read_text()
            self.assertIn('MSSQL_TLS_ENABLED=true', db)
            self.assertIn('DB_TRUST_SERVER_CERTIFICATE=false', db)
            db_password = next(line.split('=', 1)[1] for line in db.splitlines() if line.startswith('MSSQL_PASSWORD='))
            self.assertIn('DB_PASSWORD=' + db_password, be)
            for key in ('MSSQL_PASSWORD', 'MSSQL_SA_PASSWORD'):
                secret = next(line.split('=', 1)[1] for line in db.splitlines() if line.startswith(key + '='))
                self.assertNotIn(secret, result.stdout + result.stderr)
            second = self.run_setup(parent, tls)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(contents, {path: path.read_bytes() for path in envs})
            self.assertEqual(self.assert_bundle, {str(path.relative_to(tls)): path.read_bytes() for path in tls.rglob('*') if path.is_file()})
            self.assertFalse(any('ca.key' == path.name for path in parent.rglob('*')))

    def test_missing_certificate_does_not_generate_one_or_write_backend_env(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, tls = self.checkout(directory)
            (tls / 'internal/cpp/fullchain.pem').unlink()
            result = self.run_setup(parent, tls)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('certificate file is missing', result.stderr)
            self.assertFalse((tls / 'internal/cpp/fullchain.pem').exists())
            self.assertFalse((parent / 'backend/.env').exists())
            self.assertFalse((parent / 'wall/.env').exists())

    def test_wrong_key_or_hostname_fails_before_backend_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, tls = self.checkout(directory)
            original = (tls / 'internal/cpp/privkey.pem').read_bytes()
            shutil.copyfile(tls / 'internal/spring/privkey.pem', tls / 'internal/cpp/privkey.pem')
            result = self.run_setup(parent, tls)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('certificate and private key do not match', result.stderr)
            (tls / 'internal/cpp/privkey.pem').write_bytes(original)
            result = self.run_setup(parent, tls, '--public-origin', 'https://wrong.example.com')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SAN does not match', result.stderr)
            self.assertFalse((parent / 'backend/.env').exists())

    def test_plaintext_origin_is_rejected_before_any_env_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, tls = self.checkout(directory)
            result = self.run_setup(parent, tls, '--public-origin', 'http://localhost:8453')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('HTTPS origin', result.stderr)
            self.assertEqual(list(parent.rglob('.env')), [])

    @unittest.skipUnless(shutil.which('keytool'), 'JDK keytool is required')
    def test_service_tls_helper_imports_each_ca_without_copying_issuing_keys(self):
        java_home = Path(shutil.which('keytool')).resolve().parents[1]
        if not (java_home / 'lib/security/cacerts').is_file():
            self.skipTest('JDK public CA trust store is unavailable')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            script = (ROOT / 'docker/prepare-service-tls.sh').read_text()
            script = script.replace('/source-sql-ca.crt', str(self.bundle / 'mssql/ca.crt'))
            script = script.replace('/source-ca.pem', str(self.bundle / 'root-ca.pem'))
            script = script.replace('/source/', str(self.bundle / 'internal') + '/')
            script = script.replace('/target/', str(target) + '/')
            script = 'chown() { :; }\n' + script
            result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True,
                                    env={**os.environ, 'JAVA_HOME': str(java_home)})
            self.assertEqual(result.returncode, 0, result.stderr)
            inspected = subprocess.run(['keytool', '-list', '-keystore', str(target / 'spring/truststore'), '-storepass', 'changeit'], capture_output=True, text=True, check=True)
            for alias in ('irade-service-1', 'irade-service-2', 'irade-sql-1'):
                self.assertIn(alias, inspected.stdout)
            for service in ('spring', 'cpp', 'frontend'):
                self.assertEqual((target / service / 'ca.pem').read_bytes(), (self.bundle / 'root-ca.pem').read_bytes())
                self.assertEqual((target / service / 'privkey.pem').stat().st_mode & 0o777, 0o600)
            self.assertFalse(any(path.name.endswith('ca.key') for path in target.rglob('*')))

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI is required')
    def test_fresh_clone_compose_models_have_no_backend_bootstrap_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, tls = self.checkout(directory)
            result = self.run_setup(parent, tls)
            self.assertEqual(result.returncode, 0, result.stderr)
            definitions = {
                'backend': ['docker-compose.backend.yml'],
                'db': ['docker-compose.yml', 'mssql/docker-compose.yml', 'redis/docker-compose.sentinel.yml', 'elasticsearch/docker-compose.yml'],
                'wall': ['docker-compose.storage-broker.yml'],
            }
            for repo, files in definitions.items():
                for relative in files:
                    source = ROOT / relative if repo == 'backend' else ROOT.parent / repo / relative
                    shutil.copyfile(source, parent / repo / relative)
            models = {}
            for repo, filename in (('db', 'docker-compose.yml'), ('backend', 'docker-compose.backend.yml'), ('wall', 'docker-compose.storage-broker.yml')):
                checked = subprocess.run(['docker', 'compose', '-f', filename, 'config', '--format', 'json'],
                                         cwd=parent / repo, capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                models[repo] = json.loads(checked.stdout)
            database = models['db']
            self.assertFalse(database['volumes']['mssql_tls'].get('external', False))
            self.assertFalse(database['volumes']['redis-insight-tls'].get('external', False))
            self.assertEqual(database['services']['elasticsearch']['environment']['xpack.security.http.ssl.enabled'], 'true')
            self.assertIn('mssql-tls-init', database['services']['mssql']['depends_on'])
            backend = models['backend']['services']
            source_mounts = {mount['target'] for mount in backend['service-tls-init']['volumes']}
            self.assertNotIn('/target/mssql', source_mounts)
            self.assertNotIn('/target/redis-insight', source_mounts)
            self.assertIn('/source-sql-ca.crt', source_mounts)
            for name in ('spring', 'cpp'):
                self.assertEqual(backend[name]['environment']['REDIS_TLS_ENABLED'], 'true')
                self.assertFalse(backend[name].get('ports'))
                self.assertEqual(set(backend[name]['networks']), {'irade-services', 'oauth-egress'} if name == 'spring' else {'irade-services'})
                self.assertTrue(backend[name]['environment']['REDIS_BROKER_ROUTES'])
            wall = models['wall']
            proxy = wall['services']['storage-broker']
            self.assertEqual(set(proxy['networks']), {'irade-services', 'irade-net', 'redis-ha'})
            self.assertTrue(wall['networks']['irade-services']['internal'])
            self.assertFalse(proxy.get('ports'))
            self.assertTrue(all(key.startswith('STORAGE_') for key in proxy['environment']))

    def test_public_tls_split_lets_encrypt_sync_and_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, tls = self.checkout(directory)
            public_root = Path(directory) / 'public-ca'
            public_root.mkdir()
            le_ca = public_root / 'isrg-test-root.pem'
            le_ca_key = public_root / 'isrg-test-root.key'
            self.openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '90',
                         '-subj', '/CN=Public Test LE CA', '-keyout', str(le_ca_key), '-out', str(le_ca))
            source_le = public_root / 'live'
            source_le.mkdir()
            ext = public_root / 'leaf.cnf'
            ext.write_text('basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:proof-notebook.p-e.kr\n')
            csr = public_root / 'leaf.csr'
            self.openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                         '-subj', '/CN=proof-notebook.p-e.kr',
                         '-keyout', str(source_le / 'privkey.pem'), '-out', str(csr))
            self.openssl('x509', '-req', '-in', str(csr), '-CA', str(le_ca), '-CAkey', str(le_ca_key),
                         '-set_serial', '77', '-days', '90', '-extfile', str(ext), '-out', str(source_le / 'fullchain.pem'))

            synced_le = Path(directory) / 'synced-le'
            public_tls_script = ROOT.parent / 'wall/scripts/public-tls.sh'
            bad_san = subprocess.run(
                [str(public_tls_script), 'sync', 'wrong.example.com',
                 '--source-dir', str(source_le), '--dest-dir', str(synced_le), '--no-reload'],
                capture_output=True, text=True,
            )
            self.assertNotEqual(bad_san.returncode, 0)
            self.assertIn('SAN does not match', bad_san.stderr)

            synced = subprocess.run(
                [str(public_tls_script), 'sync', 'proof-notebook.p-e.kr',
                 '--source-dir', str(source_le), '--dest-dir', str(synced_le), '--no-reload'],
                capture_output=True, text=True,
            )
            self.assertEqual(synced.returncode, 0, synced.stderr)
            self.assertTrue((synced_le / 'fullchain.pem').is_file())
            self.assertTrue((synced_le / 'privkey.pem').is_file())
            self.assertFalse((synced_le / 'root-ca.pem').exists())

            untrusted = self.run_setup(
                parent, tls,
                '--public-origin', 'https://proof-notebook.p-e.kr',
                '--public-tls-dir', str(synced_le),
            )
            self.assertNotEqual(untrusted.returncode, 0)
            self.assertIn('system CAs or local CA', untrusted.stderr)

            ok = self.run_setup(
                parent, tls,
                '--public-origin', 'https://proof-notebook.p-e.kr',
                '--public-tls-dir', str(synced_le),
                extra_env={'SSL_CERT_FILE': str(le_ca)},
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)

            be = (parent / 'backend/.env').read_text()
            self.assertIn(f'NGINX_TLS_CERT_DIR="{tls}"', be)
            self.assertIn(f'NGINX_PUBLIC_TLS_CERT_DIR={synced_le.resolve()}', be)
            self.assertIn('NGINX_PUBLIC_HOSTNAME=proof-notebook.p-e.kr', be)
            self.assertIn('NGINX_EDGE_TRUST=system', be)
            self.assertIn('NGINX_HTTPS_PORT=443', be)
            self.assertIn('PASSKEY_RP_ID=proof-notebook.p-e.kr', be)
            self.assertIn('PASSKEY_ORIGIN=https://proof-notebook.p-e.kr', be)
            self.assertIn('GOOGLE_REDIRECT_URI=https://proof-notebook.p-e.kr/api/auth/oauth2/callback/google', be)
            self.assertIn('KAKAO_REDIRECT_URI=https://proof-notebook.p-e.kr/api/auth/oauth2/callback/kakao', be)

            wall = (parent / 'wall/.env').read_text()
            self.assertIn(f'NGINX_TLS_CERT_DIR="{tls}"', wall)
            self.assertIn(f'NGINX_PUBLIC_TLS_CERT_DIR="{synced_le.resolve()}"', wall)
            self.assertIn('NGINX_PUBLIC_HOSTNAME="proof-notebook.p-e.kr"', wall)
            self.assertIn('NGINX_EDGE_TRUST="system"', wall)
            self.assertIn('NGINX_HTTPS_PORT="443"', wall)


if __name__ == '__main__':
    unittest.main()
