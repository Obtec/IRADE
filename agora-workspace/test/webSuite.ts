import * as vscode from 'vscode';
import { AgoraClient, type FileRecord } from '../src/client.ts';
import { SharedDocument } from '../src/document.ts';

function check(value: boolean, message: string): asserts value { if (!value) { throw new Error(message); } }
async function until(predicate: () => boolean): Promise<void> {
	const deadline = Date.now() + 15000;
	while (!predicate()) {
		if (Date.now() > deadline) { throw new Error('Timed out waiting for editor synchronization'); }
		await new Promise(resolve => setTimeout(resolve, 50));
	}
}

/** Runs inside the real VS Code web extension host against isolated guest files. */
export async function run(): Promise<void> {
	await vscode.extensions.getExtension('irade.agora-workspace')!.activate();
	await vscode.commands.executeCommand('agora.setToken', { origin: 'https://localhost:9449', token: 'fixture-writer' });
	const root = vscode.Uri.parse('agora://42@localhost:9449/');
	const source = vscode.Uri.joinPath(root, 'project', 'proof.lean');
	await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(root, 'project'));
	await vscode.workspace.fs.writeFile(source, new TextEncoder().encode('a\nb\n'));
	const document = await vscode.workspace.openTextDocument(source);
	const editor = await vscode.window.showTextDocument(document);
	const second = new AgoraClient({ origin: 'https://localhost:9449', canvasId: 42 }, async () => 'fixture-writer');
	try {
		const record = await second.request<FileRecord>({ operation: 'read', path: '/project/proof.lean' });
		const remote = new SharedDocument(record.document!);
		remote.setText('a\nBB\n');
		check(await editor.edit(edit => edit.replace(new vscode.Range(0, 0, 0, 1), 'AA')), 'Local edit failed');
		await second.request({ operation: 'write', path: '/project/proof.lean', fileId: record.fileId, document: remote.snapshot });
		await until(() => document.getText() === 'AA\nBB\n');
		check(await document.save(), 'Shared document save failed');
		const saved = await second.request<FileRecord>({ operation: 'read', path: '/project/proof.lean' });
		check(new SharedDocument(saved.document!).text === 'AA\nBB\n', 'VM contents differ from editor');
		// A clean working copy may reload through the file provider as well as the live editor path.
		const next = new SharedDocument(saved.document!); next.setText('AA\nCC\n');
		await second.request({ operation: 'write', path: '/project/proof.lean', fileId: saved.fileId, document: next.snapshot });
		await until(() => document.getText() === 'AA\nCC\n');
		check(await editor.edit(edit => edit.replace(new vscode.Range(0, 0, 0, 2), 'ZZ')), 'Edit after remote reload failed');
		check(await document.save(), 'Save after remote reload failed');
		const destination = vscode.Uri.joinPath(root, 'project', 'renamed.lean');
		await vscode.commands.executeCommand('workbench.action.closeAllEditors');
		await vscode.workspace.fs.rename(source, destination);
		check(new TextDecoder().decode(await vscode.workspace.fs.readFile(destination)) === 'ZZ\nCC\n', 'Rename lost contents');
		await vscode.workspace.fs.delete(vscode.Uri.joinPath(root, 'project'), { recursive: true });
		check((await vscode.workspace.fs.readDirectory(root)).length === 0, 'Recursive delete failed');
		await new Promise(resolve => setTimeout(resolve, 300));
		console.log('AGORA_WEB_TEST_PASSED: file CRUD, two replicas, editor convergence, save, rename and delete');
	} finally { second.dispose(); }
}
