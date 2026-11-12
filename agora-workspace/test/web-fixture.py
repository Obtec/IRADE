"""Local web test bridge to the actual guest implementation. Never uses a real VM."""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location('workspace', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
workspace = module.Workspace(sys.argv[2], sys.argv[3])
try:
    for line in sys.stdin:
        try:
            result = workspace.handle(json.loads(line))
        except module.WorkspaceError as error:
            result = {'error': str(error)}
        except FileNotFoundError:
            result = {'error': 'FILE_NOT_FOUND'}
        except Exception as error:
            result = {'error': type(error).__name__}
        print(json.dumps(result), flush=True)
finally:
    workspace.close()
