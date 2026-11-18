export type ParticleFieldVariant = 'home' | 'login';

export interface ParticleFieldOptions {
	variant?: ParticleFieldVariant;
	densityDivisor?: number;
}

interface Particle {
	x: number;
	y: number;
	vx: number;
	vy: number;
	radius: number;
	alpha: number;
	phase: number;
	depth: number;
	color: string;
}

const PALETTES: Record<ParticleFieldVariant, string[]> = {
	home: ['#34d399', '#38bdf8', '#a78bfa', '#e2e8f0', '#6ee7b7'],
	login: ['#34d399', '#38bdf8', '#6ee7b7', '#cbd5e1', '#818cf8'],
};

/**
 * Interactive particle field canvas for Homepage and Login landing pages.
 * Renders multi-depth particles, proximity links, and gentle cursor attraction.
 */
export function initParticleField(
	canvasSelector: string,
	options: ParticleFieldOptions = {},
): () => void {
	const canvas = document.querySelector<HTMLCanvasElement>(canvasSelector);
	if (!canvas) { return () => {}; }
	const ctx = canvas.getContext('2d');
	if (!ctx) { return () => {}; }

	const variant: ParticleFieldVariant = options.variant || 'home';
	const colors = PALETTES[variant];
	const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	const dpr = Math.min(window.devicePixelRatio || 1, 2);

	let width = window.innerWidth;
	let height = window.innerHeight;
	let pointerX = -9999;
	let pointerY = -9999;
	let pointerActive = false;
	let rafId = 0;
	let lastTime = performance.now();

	const divisor = options.densityDivisor ?? (variant === 'login' ? 19000 : 17500);
	const targetCount = (): number => Math.min(108, Math.max(46, Math.round((width * height) / divisor)));

	const createParticle = (): Particle => {
		const depth = 0.35 + Math.random() * 0.65;
		const baseSpeed = 0.14 + depth * 0.24;
		const angle = Math.random() * Math.PI * 2;
		return {
			x: Math.random() * width,
			y: Math.random() * height,
			vx: Math.cos(angle) * baseSpeed,
			vy: Math.sin(angle) * baseSpeed - 0.045 * depth,
			radius: 1.0 + depth * 1.9,
			alpha: 0.26 + depth * 0.56,
			phase: Math.random() * Math.PI * 2,
			depth,
			color: colors[Math.floor(Math.random() * colors.length)],
		};
	};

	const particles: Particle[] = [];
	const syncCount = (): void => {
		const want = targetCount();
		while (particles.length < want) { particles.push(createParticle()); }
		if (particles.length > want) { particles.length = want; }
	};

	const resize = (): void => {
		width = window.innerWidth;
		height = window.innerHeight;
		canvas.width = Math.floor(width * dpr);
		canvas.height = Math.floor(height * dpr);
		canvas.style.width = `${width}px`;
		canvas.style.height = `${height}px`;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		syncCount();
	};
	resize();

	const onPointerMove = (event: PointerEvent): void => {
		if (event.pointerType === 'touch') { return; }
		pointerX = event.clientX;
		pointerY = event.clientY;
		pointerActive = true;
	};
	const onPointerLeave = (): void => {
		pointerActive = false;
		pointerX = -9999;
		pointerY = -9999;
	};

	const linkRadius = variant === 'login' ? 148 : 155;
	const pointerRadius = 185;

	const renderFrame = (now: number): void => {
		rafId = 0;
		const dt = Math.min(32, Math.max(8, now - lastTime)) / 16.6667;
		lastTime = now;

		ctx.clearRect(0, 0, width, height);

		if (pointerActive && !reducedMotion) {
			const aura = ctx.createRadialGradient(pointerX, pointerY, 0, pointerX, pointerY, pointerRadius);
			aura.addColorStop(0, variant === 'login' ? 'rgba(52, 211, 153, 0.14)' : 'rgba(56, 189, 248, 0.12)');
			aura.addColorStop(0.55, 'rgba(52, 211, 153, 0.04)');
			aura.addColorStop(1, 'rgba(0, 0, 0, 0)');
			ctx.fillStyle = aura;
			ctx.beginPath();
			ctx.arc(pointerX, pointerY, pointerRadius, 0, Math.PI * 2);
			ctx.fill();
		}

		for (const p of particles) {
			if (!reducedMotion) {
				p.phase += 0.015 * dt;
				p.x += (p.vx + Math.sin(p.phase) * 0.06) * dt;
				p.y += (p.vy + Math.cos(p.phase * 0.85) * 0.05) * dt;

				if (pointerActive) {
					const dx = pointerX - p.x;
					const dy = pointerY - p.y;
					const dist = Math.hypot(dx, dy);
					if (dist > 8 && dist < pointerRadius) {
						// Soft anti-gravity orbital lift when pointer hovers close
						const force = (1 - dist / pointerRadius) * 0.028 * p.depth * dt;
						p.vx += (dx / dist) * force;
						p.vy += (dy / dist) * force;
					}
				}

				const speed = Math.hypot(p.vx, p.vy);
				const maxSpeed = 0.42 * p.depth + 0.12;
				if (speed > maxSpeed) {
					p.vx = (p.vx / speed) * maxSpeed;
					p.vy = (p.vy / speed) * maxSpeed;
				}

				if (p.x < -24) { p.x = width + 24; }
				else if (p.x > width + 24) { p.x = -24; }
				if (p.y < -24) { p.y = height + 24; }
				else if (p.y > height + 24) { p.y = -24; }
			}
		}

		// Draw crisp network connections between close particles & hovered cursor
		for (let i = 0; i < particles.length; i += 1) {
			const a = particles[i];
			for (let j = i + 1; j < particles.length; j += 1) {
				const b = particles[j];
				const dx = a.x - b.x;
				const dy = a.y - b.y;
				const dist = Math.hypot(dx, dy);
				if (dist < linkRadius) {
					const opacity = (1 - dist / linkRadius) * 0.22 * ((a.depth + b.depth) * 0.5);
					ctx.strokeStyle = `rgba(148, 163, 184, ${opacity.toFixed(3)})`;
					ctx.lineWidth = 0.85;
					ctx.beginPath();
					ctx.moveTo(a.x, a.y);
					ctx.lineTo(b.x, b.y);
					ctx.stroke();
				}
			}
			if (pointerActive) {
				const pd = Math.hypot(pointerX - a.x, pointerY - a.y);
				if (pd < linkRadius * 1.15) {
					const opacity = (1 - pd / (linkRadius * 1.15)) * 0.36;
					ctx.strokeStyle = `rgba(52, 211, 153, ${opacity.toFixed(3)})`;
					ctx.lineWidth = 1;
					ctx.beginPath();
					ctx.moveTo(a.x, a.y);
					ctx.lineTo(pointerX, pointerY);
					ctx.stroke();
				}
			}
		}

		// Draw particles + subtle halo on bright foreground hubs
		for (const p of particles) {
			const twinkle = reducedMotion ? 1 : 0.84 + 0.16 * Math.sin(p.phase * 1.6);
			const alpha = Math.min(0.95, p.alpha * twinkle);
			ctx.globalAlpha = alpha;
			ctx.fillStyle = p.color;
			ctx.beginPath();
			ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
			ctx.fill();

			if (p.depth > 0.78) {
				ctx.globalAlpha = alpha * 0.24;
				ctx.beginPath();
				ctx.arc(p.x, p.y, p.radius * 2.7, 0, Math.PI * 2);
				ctx.fill();
			}
		}
		ctx.globalAlpha = 1;

		if (!reducedMotion && !document.hidden) {
			rafId = requestAnimationFrame(renderFrame);
		}
	};

	const onVisibilityChange = (): void => {
		if (document.hidden) {
			if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
		} else if (!reducedMotion && !rafId) {
			lastTime = performance.now();
			rafId = requestAnimationFrame(renderFrame);
		}
	};

	globalThis.addEventListener('resize', resize, { passive: true });
	window.addEventListener('pointermove', onPointerMove, { passive: true });
	window.addEventListener('pointerleave', onPointerLeave, { passive: true });
	document.addEventListener('visibilitychange', onVisibilityChange);

	renderFrame(lastTime);

	return () => {
		if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
		globalThis.removeEventListener('resize', resize);
		window.removeEventListener('pointermove', onPointerMove);
		window.removeEventListener('pointerleave', onPointerLeave);
		document.removeEventListener('visibilitychange', onVisibilityChange);
	};
}
