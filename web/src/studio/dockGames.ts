export type ArcadeGameKind = 'snake' | 'blocks';
export type SnakeDir = 'up' | 'down' | 'left' | 'right';

export interface GridPoint {
	x: number;
	y: number;
}

const SNAKE_VECTORS: Record<SnakeDir, GridPoint> = {
	up: { x: 0, y: -1 },
	down: { x: 0, y: 1 },
	left: { x: -1, y: 0 },
	right: { x: 1, y: 0 },
};

const SNAKE_OPPOSITE: Record<SnakeDir, SnakeDir> = {
	up: 'down',
	down: 'up',
	left: 'right',
	right: 'left',
};

export class SnakeGame {
	readonly cols: number;
	readonly rows: number;
	snake: GridPoint[] = [];
	dir: SnakeDir = 'right';
	private dirQueue: SnakeDir[] = [];
	food: GridPoint = { x: 0, y: 0 };
	score = 0;
	best = 0;
	over = false;
	paused = true;

	constructor(cols = 24, rows = 14, best = 0, private rng: () => number = Math.random) {
		this.cols = cols;
		this.rows = rows;
		this.best = best;
		this.reset(true);
	}

	reset(startPaused = false): void {
		const cy = Math.floor(this.rows / 2);
		const cx = Math.floor(this.cols / 3);
		this.snake = [
			{ x: cx, y: cy },
			{ x: cx - 1, y: cy },
			{ x: cx - 2, y: cy },
		];
		this.dir = 'right';
		this.dirQueue = [];
		this.score = 0;
		this.over = false;
		this.paused = startPaused;
		this.spawnFood();
	}

	queueDirection(next: SnakeDir): void {
		const lastPlanned = this.dirQueue[this.dirQueue.length - 1] ?? this.dir;
		if (next === lastPlanned || SNAKE_OPPOSITE[lastPlanned] === next) {
			return;
		}
		if (this.dirQueue.length < 2) {
			this.dirQueue.push(next);
		}
	}

	spawnFood(): void {
		const occupied = new Set(this.snake.map(seg => `${seg.x},${seg.y}`));
		const free: GridPoint[] = [];
		for (let y = 0; y < this.rows; y += 1) {
			for (let x = 0; x < this.cols; x += 1) {
				if (!occupied.has(`${x},${y}`)) {
					free.push({ x, y });
				}
			}
		}
		if (!free.length) {
			this.over = true;
			return;
		}
		const pick = Math.min(free.length - 1, Math.floor(this.rng() * free.length));
		this.food = free[pick];
	}

	tickIntervalMs(): number {
		const steps = Math.floor(this.score / 30);
		return Math.max(75, 145 - steps * 6);
	}

	step(): boolean {
		if (this.over || this.paused) { return false; }
		if (this.dirQueue.length) {
			this.dir = this.dirQueue.shift()!;
		}
		const vec = SNAKE_VECTORS[this.dir];
		const head = this.snake[0];
		const nextHead = { x: head.x + vec.x, y: head.y + vec.y };

		if (nextHead.x < 0 || nextHead.x >= this.cols || nextHead.y < 0 || nextHead.y >= this.rows) {
			this.over = true;
			return false;
		}

		const willEat = nextHead.x === this.food.x && nextHead.y === this.food.y;
		const bodyLimit = willEat ? this.snake.length : this.snake.length - 1;
		for (let i = 0; i < bodyLimit; i += 1) {
			if (this.snake[i].x === nextHead.x && this.snake[i].y === nextHead.y) {
				this.over = true;
				return false;
			}
		}

		this.snake.unshift(nextHead);
		if (willEat) {
			this.score += 10;
			if (this.score > this.best) { this.best = this.score; }
			this.spawnFood();
		} else {
			this.snake.pop();
		}
		return true;
	}
}

export type PieceId = 'I' | 'O' | 'T' | 'S' | 'Z' | 'J' | 'L';

interface PieceSpec {
	id: PieceId;
	color: string;
	shape: number[][];
}

// IRADE's own look: one teal palette (no per-piece rainbow scheme), a 12×16 board and no ghost piece, so the
// game shares only the generic falling-block rules with other games, not their name or trade dress.
const PIECES: Record<PieceId, PieceSpec> = {
	I: { id: 'I', color: '#2dd4bf', shape: [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]] },
	O: { id: 'O', color: '#14b8a6', shape: [[1, 1], [1, 1]] },
	T: { id: 'T', color: '#5eead4', shape: [[0, 1, 0], [1, 1, 1], [0, 0, 0]] },
	S: { id: 'S', color: '#0d9488', shape: [[0, 1, 1], [1, 1, 0], [0, 0, 0]] },
	Z: { id: 'Z', color: '#99f6e4', shape: [[1, 1, 0], [0, 1, 1], [0, 0, 0]] },
	J: { id: 'J', color: '#0f766e', shape: [[1, 0, 0], [1, 1, 1], [0, 0, 0]] },
	L: { id: 'L', color: '#22b8a8', shape: [[0, 0, 1], [1, 1, 1], [0, 0, 0]] },
};

const PIECE_KEYS: PieceId[] = ['I', 'O', 'T', 'S', 'Z', 'J', 'L'];
const LINE_POINTS = [0, 100, 300, 500, 800];

function rotateCW(matrix: number[][]): number[][] {
	const size = matrix.length;
	const next = Array.from({ length: size }, () => Array<number>(size).fill(0));
	for (let y = 0; y < size; y += 1) {
		for (let x = 0; x < size; x += 1) {
			next[x][size - 1 - y] = matrix[y][x];
		}
	}
	return next;
}

export interface ActivePiece {
	id: PieceId;
	color: string;
	shape: number[][];
	x: number;
	y: number;
}

export class BlockDropGame {
	readonly cols = 12;
	readonly rows = 16;
	board: (string | null)[][] = [];
	active!: ActivePiece;
	nextId: PieceId = 'T';
	private bag: PieceId[] = [];
	score = 0;
	best = 0;
	lines = 0;
	level = 1;
	over = false;
	paused = true;

	constructor(best = 0, private rng: () => number = Math.random) {
		this.best = best;
		this.reset(true);
	}

	private refillBag(): void {
		const copy = [...PIECE_KEYS];
		for (let i = copy.length - 1; i > 0; i -= 1) {
			const j = Math.floor(this.rng() * (i + 1));
			[copy[i], copy[j]] = [copy[j], copy[i]];
		}
		this.bag.push(...copy);
	}

	private drawNextId(): PieceId {
		if (!this.bag.length) { this.refillBag(); }
		return this.bag.shift()!;
	}

	reset(startPaused = false): void {
		this.board = Array.from({ length: this.rows }, () => Array<string | null>(this.cols).fill(null));
		this.bag = [];
		this.score = 0;
		this.lines = 0;
		this.level = 1;
		this.over = false;
		this.paused = startPaused;
		this.nextId = this.drawNextId();
		this.spawnPiece();
	}

	private spawnPiece(): void {
		const id = this.nextId;
		this.nextId = this.drawNextId();
		const spec = PIECES[id];
		const shape = spec.shape.map(row => [...row]);
		const x = Math.floor((this.cols - shape[0].length) / 2);
		const y = 0;
		this.active = { id, color: spec.color, shape, x, y };
		if (!this.fits(shape, x, y)) {
			this.over = true;
		}
	}

	fits(shape: number[][], offsetX: number, offsetY: number): boolean {
		for (let r = 0; r < shape.length; r += 1) {
			for (let c = 0; c < shape[r].length; c += 1) {
				if (!shape[r][c]) { continue; }
				const bx = offsetX + c;
				const by = offsetY + r;
				if (bx < 0 || bx >= this.cols || by >= this.rows) { return false; }
				if (by >= 0 && this.board[by][bx] !== null) { return false; }
			}
		}
		return true;
	}

	move(dx: number, dy: number): boolean {
		if (this.over || this.paused) { return false; }
		if (this.fits(this.active.shape, this.active.x + dx, this.active.y + dy)) {
			this.active.x += dx;
			this.active.y += dy;
			return true;
		}
		return false;
	}

	rotate(): boolean {
		if (this.over || this.paused) { return false; }
		const rotated = rotateCW(this.active.shape);
		for (const kickX of [0, -1, 1, -2, 2]) {
			if (this.fits(rotated, this.active.x + kickX, this.active.y)) {
				this.active.shape = rotated;
				this.active.x += kickX;
				return true;
			}
		}
		return false;
	}

	hardDrop(): number {
		if (this.over || this.paused) { return 0; }
		let dropped = 0;
		while (this.fits(this.active.shape, this.active.x, this.active.y + 1)) {
			this.active.y += 1;
			dropped += 1;
		}
		this.score += dropped * 2;
		if (this.score > this.best) { this.best = this.score; }
		this.lockPiece();
		return dropped;
	}

	step(): boolean {
		if (this.over || this.paused) { return false; }
		if (this.move(0, 1)) {
			return true;
		}
		this.lockPiece();
		return false;
	}

	private lockPiece(): void {
		const { shape, x, y, color } = this.active;
		for (let r = 0; r < shape.length; r += 1) {
			for (let c = 0; c < shape[r].length; c += 1) {
				if (!shape[r][c]) { continue; }
				const by = y + r;
				const bx = x + c;
				if (by >= 0 && by < this.rows && bx >= 0 && bx < this.cols) {
					this.board[by][bx] = color;
				}
			}
		}
		let cleared = 0;
		for (let row = this.rows - 1; row >= 0; row -= 1) {
			if (this.board[row].every(cell => cell !== null)) {
				this.board.splice(row, 1);
				this.board.unshift(Array<string | null>(this.cols).fill(null));
				cleared += 1;
				row += 1;
			}
		}
		if (cleared > 0) {
			this.lines += cleared;
			this.score += (LINE_POINTS[Math.min(4, cleared)] || 800) * this.level;
			this.level = Math.floor(this.lines / 10) + 1;
			if (this.score > this.best) { this.best = this.score; }
		}
		this.spawnPiece();
	}

	tickIntervalMs(): number {
		return Math.max(110, 680 - (this.level - 1) * 55);
	}

	nextPreviewShape(): { color: string; shape: number[][] } {
		return { color: PIECES[this.nextId].color, shape: PIECES[this.nextId].shape };
	}
}

const SNAKE_BEST_KEY = 'irade.arcade.snakeBest';
const BLOCKS_BEST_KEY = 'irade.arcade.blocksBest';
const LEGACY_BLOCKS_BEST_KEY = 'irade.arcade.tetrisBest';  // keeps scores saved before the rename

function readBest(key: string, legacyKey?: string): number {
	const raw = Number(localStorage.getItem(key) ?? (legacyKey ? localStorage.getItem(legacyKey) : null));
	return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
}

export class DockArcadeController {
	activeGame: ArcadeGameKind = 'snake';
	readonly snake: SnakeGame;
	readonly blocks: BlockDropGame;
	private visible = false;
	private timer: ReturnType<typeof setTimeout> | undefined;
	private expanded = false;

	private stageEl!: HTMLElement;
	private canvas!: HTMLCanvasElement;
	private ctx!: CanvasRenderingContext2D;
	private nextBoxEl!: HTMLElement;
	private nextCanvas!: HTMLCanvasElement;
	private nextCtx!: CanvasRenderingContext2D;
	private scoreEl!: HTMLElement;
	private bestEl!: HTMLElement;
	private metaEl!: HTMLElement;
	private stateEl!: HTMLElement;
	private toggleBtn!: HTMLButtonElement;
	private expandBtn!: HTMLButtonElement;
	private actionBtn!: HTMLButtonElement;
	private hintEl!: HTMLElement;

	constructor(
		private readonly container: HTMLElement,
		private readonly onStatusChange: (label: string) => void,
		private readonly onToggleExpand: (expanded: boolean) => void,
	) {
		this.snake = new SnakeGame(24, 14, readBest(SNAKE_BEST_KEY));
		this.blocks = new BlockDropGame(readBest(BLOCKS_BEST_KEY, LEGACY_BLOCKS_BEST_KEY));
		this.mount();
		this.bindEvents();
		this.syncUi();
	}

	private mount(): void {
		this.container.innerHTML = `<div class="dock-arcade-shell" tabindex="0" data-arcade-stage>
  <div class="dock-arcade-sidebar">
    <div class="dock-arcade-switch" role="tablist" aria-label="미니게임 선택">
      <button type="button" class="dock-game-tab selected" data-arcade-game="snake" role="tab" aria-selected="true">🐍 스네이크</button>
      <button type="button" class="dock-game-tab" data-arcade-game="blocks" role="tab" aria-selected="false">🧱 블록 낙하</button>
    </div>
    <div class="dock-arcade-stats">
      <div class="dock-stat"><span>점수</span><b data-arcade-score>0</b></div>
      <div class="dock-stat"><span>최고</span><b data-arcade-best>0</b></div>
      <div class="dock-stat"><span data-arcade-meta-label>길이</span><b data-arcade-meta>3</b></div>
    </div>
    <div class="dock-arcade-controls">
      <button type="button" class="button primary dock-arcade-btn" data-arcade-toggle>▶ 시작</button>
      <button type="button" class="button ghost dock-arcade-btn" data-arcade-reset>↻ 다시</button>
      <button type="button" class="button ghost dock-arcade-btn" data-arcade-expand title="패널 크기 키우기/줄이기">⤢ 크게</button>
    </div>
    <div class="dock-arcade-status" data-arcade-state>시작 버튼을 누르거나 방향키를 누르세요.</div>
  </div>
  <div class="dock-arcade-arena">
    <canvas class="dock-arcade-canvas" data-arcade-canvas width="360" height="210" aria-label="게임 화면"></canvas>
    <div class="dock-arcade-side-panel">
      <div class="dock-next-wrap" data-arcade-next-wrap hidden>
        <span class="dock-next-label">NEXT</span>
        <canvas class="dock-next-canvas" data-arcade-next width="68" height="68"></canvas>
      </div>
      <div class="dock-dpad" aria-label="터치/마우스 조작 패드">
        <button type="button" class="dock-pad-btn up" data-pad="up" title="위 / 회전">▲</button>
        <button type="button" class="dock-pad-btn left" data-pad="left" title="왼쪽">◀</button>
        <button type="button" class="dock-pad-btn down" data-pad="down" title="아래 / 내리기">▼</button>
        <button type="button" class="dock-pad-btn right" data-pad="right" title="오른쪽">▶</button>
      </div>
      <button type="button" class="button ghost dock-action-btn" data-arcade-action hidden>드롭 ⤓</button>
      <p class="dock-arcade-hint" data-arcade-hint>방향키 / WASD 이동 · P 일시정지</p>
    </div>
  </div>
</div>`;

		this.stageEl = this.container.querySelector<HTMLElement>('[data-arcade-stage]')!;
		this.canvas = this.container.querySelector<HTMLCanvasElement>('[data-arcade-canvas]')!;
		this.ctx = this.canvas.getContext('2d')!;
		this.nextBoxEl = this.container.querySelector<HTMLElement>('[data-arcade-next-wrap]')!;
		this.nextCanvas = this.container.querySelector<HTMLCanvasElement>('[data-arcade-next]')!;
		this.nextCtx = this.nextCanvas.getContext('2d')!;
		this.scoreEl = this.container.querySelector<HTMLElement>('[data-arcade-score]')!;
		this.bestEl = this.container.querySelector<HTMLElement>('[data-arcade-best]')!;
		this.metaEl = this.container.querySelector<HTMLElement>('[data-arcade-meta]')!;
		this.stateEl = this.container.querySelector<HTMLElement>('[data-arcade-state]')!;
		this.toggleBtn = this.container.querySelector<HTMLButtonElement>('[data-arcade-toggle]')!;
		this.expandBtn = this.container.querySelector<HTMLButtonElement>('[data-arcade-expand]')!;
		this.actionBtn = this.container.querySelector<HTMLButtonElement>('[data-arcade-action]')!;
		this.hintEl = this.container.querySelector<HTMLElement>('[data-arcade-hint]')!;
	}

	private bindEvents(): void {
		this.container.querySelectorAll<HTMLButtonElement>('[data-arcade-game]').forEach(btn => {
			btn.addEventListener('click', () => {
				const kind = btn.dataset.arcadeGame === 'blocks' ? 'blocks' : 'snake';
				this.selectGame(kind);
				this.stageEl.focus({ preventScroll: true });
			});
		});

		this.toggleBtn.addEventListener('click', () => {
			this.togglePauseOrStart();
			this.stageEl.focus({ preventScroll: true });
		});

		this.container.querySelector('[data-arcade-reset]')?.addEventListener('click', () => {
			this.resetCurrent(false);
			this.stageEl.focus({ preventScroll: true });
		});

		this.expandBtn.addEventListener('click', () => {
			this.expanded = !this.expanded;
			this.expandBtn.textContent = this.expanded ? '⤡ 기본' : '⤢ 크게';
			this.onToggleExpand(this.expanded);
			this.stageEl.focus({ preventScroll: true });
		});

		this.actionBtn.addEventListener('click', () => {
			if (this.activeGame !== 'blocks') { return; }
			if (this.blocks.over) {
				this.blocks.reset(false);
			} else if (this.blocks.paused) {
				this.blocks.paused = false;
			} else {
				this.blocks.hardDrop();
				this.persistBest();
			}
			this.scheduleLoop();
			this.syncUi();
			this.stageEl.focus({ preventScroll: true });
		});

		this.container.querySelectorAll<HTMLButtonElement>('[data-pad]').forEach(btn => {
			btn.addEventListener('click', () => {
				const dir = btn.dataset.pad as SnakeDir;
				this.handleDirectionInput(dir);
				this.stageEl.focus({ preventScroll: true });
			});
		});

		this.canvas.addEventListener('click', () => {
			this.stageEl.focus({ preventScroll: true });
			const current = this.currentEngine();
			if (current.paused && !current.over) {
				current.paused = false;
				this.scheduleLoop();
				this.syncUi();
			}
		});

		document.addEventListener('keydown', event => {
			if (!this.visible) { return; }
			const active = document.activeElement;
			const insideArcade = Boolean(active && this.container.contains(active));
			if (!insideArcade && (
				active instanceof HTMLInputElement
				|| active instanceof HTMLTextAreaElement
				|| active instanceof HTMLSelectElement
				|| active?.closest('.monaco-editor')
				|| active?.closest('dialog[open]')
			)) {
				return;
			}

			const key = event.key;
			if (key === 'ArrowUp' || key === 'w' || key === 'W') {
				event.preventDefault();
				this.handleDirectionInput('up');
			} else if (key === 'ArrowDown' || key === 's' || key === 'S') {
				event.preventDefault();
				this.handleDirectionInput('down');
			} else if (key === 'ArrowLeft' || key === 'a' || key === 'A') {
				event.preventDefault();
				this.handleDirectionInput('left');
			} else if (key === 'ArrowRight' || key === 'd' || key === 'D') {
				event.preventDefault();
				this.handleDirectionInput('right');
			} else if (key === ' ' && this.activeGame === 'blocks') {
				event.preventDefault();
				if (this.blocks.over) {
					this.blocks.reset(false);
				} else if (this.blocks.paused) {
					this.blocks.paused = false;
				} else {
					this.blocks.hardDrop();
					this.persistBest();
				}
				this.scheduleLoop();
				this.syncUi();
			} else if (key === 'p' || key === 'P') {
				event.preventDefault();
				this.togglePauseOrStart();
			}
		});
	}

	private currentEngine(): SnakeGame | BlockDropGame {
		return this.activeGame === 'snake' ? this.snake : this.blocks;
	}

	private persistBest(): void {
		if (this.activeGame === 'snake') {
			localStorage.setItem(SNAKE_BEST_KEY, String(this.snake.best));
		} else {
			localStorage.setItem(BLOCKS_BEST_KEY, String(this.blocks.best));
		}
	}

	selectGame(kind: ArcadeGameKind): void {
		if (this.activeGame !== kind) {
			this.snake.paused = true;
			this.blocks.paused = true;
			clearTimeout(this.timer);
			this.activeGame = kind;
		}
		if (kind === 'blocks') {
			this.canvas.width = 240;
			this.canvas.height = 320;
			this.canvas.classList.add('blocks-aspect');
		} else {
			this.canvas.width = 360;
			this.canvas.height = 210;
			this.canvas.classList.remove('blocks-aspect');
		}
		this.syncUi();
	}

	setVisible(nextVisible: boolean): void {
		this.visible = nextVisible;
		if (!nextVisible) {
			this.snake.paused = true;
			this.blocks.paused = true;
			clearTimeout(this.timer);
			this.timer = undefined;
		} else {
			this.syncUi();
		}
	}

	togglePauseOrStart(): void {
		const engine = this.currentEngine();
		if (engine.over) {
			engine.reset(false);
		} else {
			engine.paused = !engine.paused;
		}
		this.scheduleLoop();
		this.syncUi();
	}

	resetCurrent(startPaused = false): void {
		this.currentEngine().reset(startPaused);
		this.scheduleLoop();
		this.syncUi();
	}

	private handleDirectionInput(dir: SnakeDir): void {
		if (this.activeGame === 'snake') {
			if (this.snake.over) {
				this.snake.reset(false);
			} else if (this.snake.paused) {
				this.snake.paused = false;
			}
			this.snake.queueDirection(dir);
			this.scheduleLoop();
			this.syncUi();
			return;
		}

		if (this.blocks.over) {
			this.blocks.reset(false);
		} else if (this.blocks.paused) {
			this.blocks.paused = false;
		}
		if (dir === 'left') { this.blocks.move(-1, 0); }
		else if (dir === 'right') { this.blocks.move(1, 0); }
		else if (dir === 'down') {
			if (this.blocks.move(0, 1)) {
				this.blocks.score += 1;
				if (this.blocks.score > this.blocks.best) { this.blocks.best = this.blocks.score; }
			}
		} else if (dir === 'up') {
			this.blocks.rotate();
		}
		this.persistBest();
		this.scheduleLoop();
		this.syncUi();
	}

	private scheduleLoop(): void {
		clearTimeout(this.timer);
		this.timer = undefined;
		const engine = this.currentEngine();
		if (!this.visible || engine.paused || engine.over) { return; }
		this.timer = setTimeout(() => {
			engine.step();
			this.persistBest();
			this.syncUi();
			if (this.visible && !engine.paused && !engine.over) {
				this.scheduleLoop();
			}
		}, engine.tickIntervalMs());
	}

	syncUi(): void {
		this.container.querySelectorAll<HTMLButtonElement>('[data-arcade-game]').forEach(btn => {
			const match = btn.dataset.arcadeGame === this.activeGame;
			btn.classList.toggle('selected', match);
			btn.setAttribute('aria-selected', String(match));
		});

		const metaLabel = this.container.querySelector<HTMLElement>('[data-arcade-meta-label]');
		const engine = this.currentEngine();
		if (this.activeGame === 'snake') {
			this.scoreEl.textContent = String(this.snake.score);
			this.bestEl.textContent = String(this.snake.best);
			if (metaLabel) { metaLabel.textContent = '길이'; }
			this.metaEl.textContent = String(this.snake.snake.length);
			this.nextBoxEl.hidden = true;
			this.actionBtn.hidden = true;
			this.hintEl.textContent = '방향키 / WASD 이동 · P 일시정지';
			this.onStatusChange(`스네이크 · ${this.snake.score}점 (최고 ${this.snake.best})`);
		} else {
			this.scoreEl.textContent = String(this.blocks.score);
			this.bestEl.textContent = String(this.blocks.best);
			if (metaLabel) { metaLabel.textContent = '라인'; }
			this.metaEl.textContent = `${this.blocks.lines} (Lv.${this.blocks.level})`;
			this.nextBoxEl.hidden = false;
			this.actionBtn.hidden = false;
			this.hintEl.textContent = '↑ 회전 · ←→ 이동 · ↓ 소프트드롭 · Space 드롭';
			this.onStatusChange(`블록 낙하 · Lv.${this.blocks.level} · ${this.blocks.score}점`);
		}

		this.toggleBtn.textContent = engine.over ? '↻ 재시작' : engine.paused ? '▶ 시작' : '❚❚ 일시정지';
		this.stateEl.textContent = engine.over
			? '게임 오버! 다시 시작을 누르세요.'
			: engine.paused
				? '일시정지 · 시작 또는 방향키로 플레이'
				: '플레이 중';
		this.stateEl.classList.toggle('over', engine.over);
		this.draw();
	}

	private draw(): void {
		const isLight = document.documentElement.getAttribute('data-theme') === 'light';
		const bg = isLight ? '#f3f4f6' : '#111318';
		const gridColor = isLight ? 'rgba(15, 23, 42, 0.07)' : 'rgba(255, 255, 255, 0.05)';
		const textPrimary = isLight ? '#111827' : '#f8fafc';
		const overlayBg = isLight ? 'rgba(255, 255, 255, 0.82)' : 'rgba(15, 18, 25, 0.78)';

		if (this.activeGame === 'snake') {
			const cellW = this.canvas.width / this.snake.cols;
			const cellH = this.canvas.height / this.snake.rows;
			this.ctx.fillStyle = bg;
			this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

			this.ctx.strokeStyle = gridColor;
			this.ctx.lineWidth = 1;
			for (let x = 1; x < this.snake.cols; x += 1) {
				this.ctx.beginPath();
				this.ctx.moveTo(x * cellW, 0);
				this.ctx.lineTo(x * cellW, this.canvas.height);
				this.ctx.stroke();
			}
			for (let y = 1; y < this.snake.rows; y += 1) {
				this.ctx.beginPath();
				this.ctx.moveTo(0, y * cellH);
				this.ctx.lineTo(this.canvas.width, y * cellH);
				this.ctx.stroke();
			}

			// Food orb
			const fx = (this.snake.food.x + 0.5) * cellW;
			const fy = (this.snake.food.y + 0.5) * cellH;
			this.ctx.fillStyle = '#f43f5e';
			this.ctx.beginPath();
			this.ctx.arc(fx, fy, Math.min(cellW, cellH) * 0.36, 0, Math.PI * 2);
			this.ctx.fill();

			// Snake segments
			this.snake.snake.forEach((seg, idx) => {
				const px = seg.x * cellW + 1.5;
				const py = seg.y * cellH + 1.5;
				const w = cellW - 3;
				const h = cellH - 3;
				this.ctx.fillStyle = idx === 0 ? '#34d399' : idx % 2 === 0 ? '#10b981' : '#059669';
				this.ctx.beginPath();
				this.ctx.roundRect(px, py, w, h, 4);
				this.ctx.fill();
			});

			if (this.snake.over || this.snake.paused) {
				this.drawOverlay(overlayBg, textPrimary, this.snake.over ? `GAME OVER · ${this.snake.score}점` : 'SNAKE READY');
			}
			return;
		}

		// Block-drop render
		const cellSize = this.canvas.width / this.blocks.cols;
		this.ctx.fillStyle = bg;
		this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

		this.ctx.strokeStyle = gridColor;
		this.ctx.lineWidth = 1;
		for (let c = 1; c < this.blocks.cols; c += 1) {
			this.ctx.beginPath();
			this.ctx.moveTo(c * cellSize, 0);
			this.ctx.lineTo(c * cellSize, this.canvas.height);
			this.ctx.stroke();
		}
		for (let r = 1; r < this.blocks.rows; r += 1) {
			this.ctx.beginPath();
			this.ctx.moveTo(0, r * cellSize);
			this.ctx.lineTo(this.canvas.width, r * cellSize);
			this.ctx.stroke();
		}

		const drawCell = (cx: number, cy: number, color: string, alpha = 1): void => {
			if (cy < 0) { return; }
			this.ctx.save();
			this.ctx.globalAlpha = alpha;
			this.ctx.fillStyle = color;
			this.ctx.beginPath();
			this.ctx.roundRect(cx * cellSize + 1, cy * cellSize + 1, cellSize - 2, cellSize - 2, 3);
			this.ctx.fill();
			this.ctx.restore();
		};

		for (let r = 0; r < this.blocks.rows; r += 1) {
			for (let c = 0; c < this.blocks.cols; c += 1) {
				const cellColor = this.blocks.board[r][c];
				if (cellColor) { drawCell(c, r, cellColor); }
			}
		}

		// Active piece
		for (let r = 0; r < this.blocks.active.shape.length; r += 1) {
			for (let c = 0; c < this.blocks.active.shape[r].length; c += 1) {
				if (this.blocks.active.shape[r][c]) {
					drawCell(this.blocks.active.x + c, this.blocks.active.y + r, this.blocks.active.color);
				}
			}
		}

		// Next preview
		this.nextCtx.fillStyle = bg;
		this.nextCtx.fillRect(0, 0, this.nextCanvas.width, this.nextCanvas.height);
		const preview = this.blocks.nextPreviewShape();
		const block = 13;
		const offsetX = Math.floor((this.nextCanvas.width - preview.shape[0].length * block) / 2);
		const offsetY = Math.floor((this.nextCanvas.height - preview.shape.length * block) / 2);
		for (let r = 0; r < preview.shape.length; r += 1) {
			for (let c = 0; c < preview.shape[r].length; c += 1) {
				if (preview.shape[r][c]) {
					this.nextCtx.fillStyle = preview.color;
					this.nextCtx.beginPath();
					this.nextCtx.roundRect(offsetX + c * block + 1, offsetY + r * block + 1, block - 2, block - 2, 2);
					this.nextCtx.fill();
				}
			}
		}

		if (this.blocks.over || this.blocks.paused) {
			this.drawOverlay(overlayBg, textPrimary, this.blocks.over ? `GAME OVER · ${this.blocks.score}점` : 'BLOCK DROP READY');
		}
	}

	private drawOverlay(bg: string, fg: string, title: string): void {
		this.ctx.fillStyle = bg;
		this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
		this.ctx.fillStyle = fg;
		this.ctx.textAlign = 'center';
		this.ctx.textBaseline = 'middle';
		this.ctx.font = '700 13px Inter, system-ui, sans-serif';
		this.ctx.fillText(title, this.canvas.width / 2, this.canvas.height / 2);
	}
}
