import { initParticleField } from './particleField.ts';

const STAGE_DURATION_MS = 4000;

function initConstellation(): void {
	initParticleField('#hero-constellation', { variant: 'home' });
}

function initStageChoreography(): void {
	const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>('[data-stage]'));
	const panels = Array.from(document.querySelectorAll<HTMLElement>('[data-stage-panel]'));
	const progressFill = document.querySelector<HTMLElement>('#stage-progress-fill');
	const badge = document.querySelector<HTMLElement>('#stage-sync-badge');
	if (!tabs.length || !panels.length) { return; }

	const badges = ['AGENT ACTIVE', 'PROOF VERIFIED', 'VM RUNNING', 'LIVE SYNC'];
	let current = 0;
	let timer: ReturnType<typeof setInterval> | undefined;

	const activate = (index: number): void => {
		current = ((index % tabs.length) + tabs.length) % tabs.length;
		tabs.forEach((tab, idx) => {
			const selected = idx === current;
			tab.classList.toggle('active', selected);
			tab.setAttribute('aria-selected', String(selected));
		});
		panels.forEach((panel, idx) => {
			panel.classList.toggle('active', idx === current);
		});
		if (progressFill) {
			progressFill.style.width = `${((current + 1) / tabs.length) * 100}%`;
		}
		if (badge) {
			badge.textContent = badges[current] || 'LIVE SYNC';
		}
	};

	const startCycle = (): void => {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { return; }
		clearInterval(timer);
		timer = setInterval(() => activate(current + 1), STAGE_DURATION_MS);
	};

	tabs.forEach((tab, idx) => {
		tab.addEventListener('click', () => {
			activate(idx);
			startCycle();
		});
	});

	activate(0);
	startCycle();
}

function initScrollReveal(): void {
	const targets = document.querySelectorAll<HTMLElement>('.reveal-on-scroll');
	if (!targets.length) { return; }
	if (!('IntersectionObserver' in window)) {
		targets.forEach(el => el.classList.add('visible'));
		return;
	}
	const observer = new IntersectionObserver(entries => {
		for (const entry of entries) {
			if (entry.isIntersecting) {
				entry.target.classList.add('visible');
				observer.unobserve(entry.target);
			}
		}
	}, { threshold: 0.14 });
	targets.forEach(el => observer.observe(el));
}

initConstellation();
initStageChoreography();
initScrollReveal();
