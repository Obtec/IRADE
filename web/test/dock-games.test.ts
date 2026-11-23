import test from 'node:test';
import assert from 'node:assert/strict';
import { BlockDropGame, SnakeGame } from '../src/studio/dockGames.ts';

test('SnakeGame moves, grows on food, blocks 180-degree reversal, and detects wall collisions', () => {
	const game = new SnakeGame(12, 8, 0, () => 0);
	game.reset(false);
	const head = game.snake[0];
	// Place food directly in front of snake head and step forward.
	game.food = { x: head.x + 1, y: head.y };
	game.queueDirection('left'); // illegal 180 reversal from 'right' must be ignored
	assert.equal(game.step(), true);
	assert.equal(game.score, 10);
	assert.equal(game.best, 10);
	assert.equal(game.snake.length, 4);
	assert.equal(game.dir, 'right');

	// Drive into right wall -> game over.
	for (let i = 0; i < 20 && !game.over; i += 1) {
		game.step();
	}
	assert.equal(game.over, true);
});

test('BlockDropGame moves, rotates, hard-drops with points, and clears full lines', () => {
	const game = new BlockDropGame(0, () => 0.1);
	game.reset(false);
	assert.equal(game.cols, 12);
	assert.equal(game.rows, 16);
	game.move(1, 0);
	game.rotate();
	const dropped = game.hardDrop();
	assert.ok(dropped > 0);
	assert.ok(game.score >= dropped * 2);

	// Fill the bottom row cleanly except col 0..3 and drop an 'I' horizontal bar cleanly to verify line clear.
	for (let r = 0; r < game.rows; r += 1) {
		game.board[r].fill(null);
	}
	for (let c = 4; c < game.cols; c += 1) {
		game.board[game.rows - 1][c] = '#2dd4bf';
	}
	game.active = {
		id: 'I',
		color: '#2dd4bf',
		shape: [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]],
		x: 0,
		y: 0,
	};
	game.score = 0;
	game.lines = 0;
	game.hardDrop();
	assert.equal(game.lines, 1);
	assert.ok(game.score >= 100);
});
