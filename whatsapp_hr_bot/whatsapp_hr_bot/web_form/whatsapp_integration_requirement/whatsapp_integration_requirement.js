frappe.ready(function () {
	const styleId = "whatsapp-integration-form-styles";

	if (!document.getElementById(styleId)) {
		const style = document.createElement("style");
		style.id = styleId;
		style.textContent = `
			:root {
				--wa-ink: #172a2b;
				--wa-muted: #647575;
				--wa-teal: #167d75;
				--wa-teal-dark: #0e5f59;
				--wa-mint: #e4f2ed;
				--wa-paper: #fbfaf6;
				--wa-line: #dce6e1;
			}

			body:has(.web-form-container) {
				background: #f1f5f2;
				color: var(--wa-ink);
				font-family: "Avenir Next", "Segoe UI", sans-serif;
			}

			.web-form-container {
				max-width: 980px;
				padding: 18px 24px 64px;
			}

			.web-form-head {
				margin: 22px 0 28px;
				text-align: left;
			}

			.web-form-head h1,
			.web-form-head .title {
				color: var(--wa-ink);
				font-size: clamp(2rem, 4vw, 3.15rem);
				font-weight: 700;
				letter-spacing: -0.035em;
				line-height: 1.05;
				margin-bottom: 12px;
			}

			.web-form-head p {
				color: var(--wa-muted);
				font-size: 1rem;
				line-height: 1.65;
				max-width: 650px;
			}

			.wa-form-banner {
				align-items: center;
				background: var(--wa-ink);
				border-radius: 12px;
				color: white;
				display: flex;
				gap: 18px;
				margin: 0 0 28px;
				padding: 18px 22px;
			}

			.wa-form-banner-mark {
				align-items: center;
				background: #d3f1e5;
				border-radius: 50%;
				color: var(--wa-teal-dark);
				display: flex;
				flex: 0 0 42px;
				font-size: 1.25rem;
				height: 42px;
				justify-content: center;
				width: 42px;
			}

			.wa-form-banner strong {
				display: block;
				font-size: 0.98rem;
				margin-bottom: 3px;
			}

			.wa-form-banner span {
				color: #c3d8d2;
				font-size: 0.86rem;
				line-height: 1.45;
			}

			.web-form .form-section {
				background: var(--wa-paper);
				border: 1px solid var(--wa-line);
				border-radius: 10px;
				box-shadow: 0 5px 18px rgba(23, 42, 43, 0.035);
				margin-bottom: 18px;
				padding: 24px 26px 8px;
			}

			.web-form .section-head {
				border-bottom: 1px solid var(--wa-line);
				color: var(--wa-ink);
				font-size: 1.18rem;
				font-weight: 700;
				margin: -2px 0 22px;
				padding-bottom: 14px;
			}

			.web-form .section-head:before {
				background: var(--wa-teal);
				border-radius: 2px;
				content: "";
				display: inline-block;
				height: 18px;
				margin-right: 10px;
				vertical-align: -3px;
				width: 4px;
			}

			.web-form .form-group {
				margin-bottom: 20px;
			}

			.web-form label {
				color: #385252;
				font-size: 0.8rem;
				font-weight: 700;
				letter-spacing: 0.02em;
				margin-bottom: 7px;
				text-transform: uppercase;
			}

			.web-form .form-control,
			.web-form select,
			.web-form textarea {
				background: white;
				border: 1px solid #cbd9d4;
				border-radius: 6px;
				box-shadow: none;
				min-height: 44px;
				padding: 10px 12px;
				transition: border-color 160ms ease, box-shadow 160ms ease;
			}

			.web-form .form-control:focus,
			.web-form select:focus,
			.web-form textarea:focus {
				border-color: var(--wa-teal);
				box-shadow: 0 0 0 3px rgba(22, 125, 117, 0.13);
				outline: none;
			}

			.web-form .help-box,
			.web-form .desc {
				color: var(--wa-muted);
				font-size: 0.78rem;
				line-height: 1.5;
				margin-top: 6px;
			}

			.web-form .table thead th {
				background: var(--wa-mint);
				border-bottom: 0;
				color: #31504d;
				font-size: 0.75rem;
				font-weight: 700;
				text-transform: uppercase;
			}

			.web-form .btn-primary {
				background: var(--wa-teal);
				border: 0;
				border-radius: 6px;
				box-shadow: 0 5px 12px rgba(22, 125, 117, 0.2);
				font-weight: 700;
				padding: 12px 24px;
				transition: background 160ms ease, transform 160ms ease;
			}

			.web-form .btn-primary:hover {
				background: var(--wa-teal-dark);
				transform: translateY(-1px);
			}

			@media (max-width: 600px) {
				.web-form-container { padding: 8px 14px 42px; }
				.web-form .form-section { padding: 20px 16px 4px; }
				.wa-form-banner { align-items: flex-start; padding: 16px; }
			}
		`;
		document.head.appendChild(style);
	}

	const formHead = document.querySelector(".web-form-head");
	if (formHead && !document.querySelector(".wa-form-banner")) {
		const banner = document.createElement("div");
		banner.className = "wa-form-banner";
		banner.innerHTML = `
			<div class="wa-form-banner-mark" aria-hidden="true">WA</div>
			<div>
				<strong>Plan your WhatsApp workflow</strong>
				<span>Share the messages, triggers, and conversations your team needs. We will shape the right integration around them.</span>
			</div>
		`;
		formHead.insertAdjacentElement("afterend", banner);
	}
});
