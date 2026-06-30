/**
 * Smart Price Tracker — Frontend SPA
 * All data fetched from the Flask API; no page reloads.
 *
 * Features:
 *  - Product list with sparklines, savings badge, delete
 *  - Add product form with threshold slider
 *  - Full price history modal (Chart.js line chart + summary stats)
 *  - Alert management inside history modal (view / reactivate / delete alerts)
 *  - Live search/filter across product names and websites
 *  - Manual price refresh with loading spinner
 *  - Dark mode toggle (persisted in localStorage)
 *  - Scheduler status in footer
 *  - Toast notifications for all actions
 */

'use strict';

// ── Global state ──────────────────────────────────────────────────────────────
let historyChartInstance = null;
let sparklineInstances   = {};
let allProducts          = [];

// ── Bootstrap ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  applyStoredTheme();
  loadStats();
  loadProducts();
  loadSchedulerStatus();
  setInterval(loadSchedulerStatus, 60_000);
});

// ── Mode toggle (auto / manual) ────────────────────────────────────────────────
function setMode(mode) {
  const autoForm   = document.getElementById('addForm');
  const manualForm = document.getElementById('manualForm');
  const btnAuto    = document.getElementById('modeAuto');
  const btnManual  = document.getElementById('modeManual');

  if (mode === 'manual') {
    autoForm.style.display   = 'none';
    manualForm.style.display = '';
    btnAuto.classList.remove('active');
    btnManual.classList.add('active');
  } else {
    autoForm.style.display   = '';
    manualForm.style.display = 'none';
    btnAuto.classList.add('active');
    btnManual.classList.remove('active');
  }
}

// Auto-switch to manual mode if the error message mentions Ajio/bot-blocked
function maybeAutoSwitchManual(errorMsg) {
  if (/ajio|akamai|blocked|manual entry/i.test(errorMsg)) {
    setMode('manual');
    showToast('Switched to Manual Entry mode — paste the product details below', 'warning', 6000);
    // Pre-fill the manual URL from the auto URL field
    const autoUrl = document.getElementById('productUrl')?.value;
    if (autoUrl) document.getElementById('manualUrl').value = autoUrl;
  }
}

// ── Dark mode ──────────────────────────────────────────────────────────────────
function applyStoredTheme() {
  if (localStorage.getItem('theme') === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    const icon = document.getElementById('themeIcon');
    if (icon) icon.textContent = '☀️';
  }
}

function toggleTheme() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
  localStorage.setItem('theme', isDark ? 'light' : 'dark');
  const icon = document.getElementById('themeIcon');
  if (icon) icon.textContent = isDark ? '🌙' : '☀️';
}

// ── Scheduler status (footer) ─────────────────────────────────────────────────
async function loadSchedulerStatus() {
  try {
    const res  = await fetch('/api/scheduler-status');
    const data = await res.json();
    const el   = document.getElementById('schedulerStatus');
    if (!el) return;
    if (data.next_run) {
      el.textContent =
        `⏰ Next auto-check: ${formatDateTime(data.next_run)} (every ${data.interval_hours}h)`;
    } else {
      el.textContent = data.running ? '⏰ Scheduler running' : '⚠️ Scheduler not running';
    }
  } catch {
    /* non-critical — ignore */
  }
}

// ── Stats bar ──────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('statProducts').textContent = data.total_products;
    document.getElementById('statAlerts').textContent   = data.active_alerts;
    document.getElementById('statSavings').textContent  =
      '₹' + formatNumber(data.total_savings);
  } catch (err) {
    console.error('Failed to load stats:', err);
  }
}

// ── Product list ───────────────────────────────────────────────────────────────
async function loadProducts() {
  try {
    const res = await fetch('/api/products');
    allProducts = await res.json();
    applyFilter();
  } catch (err) {
    console.error('Failed to load products:', err);
    showToast('Failed to load products', 'error');
  }
}

/** Apply current search query against allProducts and re-render. */
function applyFilter() {
  const query = (document.getElementById('searchInput')?.value || '').toLowerCase().trim();
  const filtered = query
    ? allProducts.filter(p =>
        p.name.toLowerCase().includes(query) ||
        p.website.toLowerCase().includes(query)
      )
    : allProducts;
  renderProducts(filtered);
  updateProductCount(allProducts.length, filtered.length);
}

function renderProducts(products) {
  const grid  = document.getElementById('productGrid');
  const empty = document.getElementById('emptyState');

  // Destroy existing sparklines before replacing DOM
  Object.values(sparklineInstances).forEach(c => c.destroy());
  sparklineInstances = {};

  if (!products.length) {
    grid.innerHTML = '';
    grid.appendChild(empty);
    // Update empty-state message for search vs truly empty
    const hasSearch = (document.getElementById('searchInput')?.value || '').trim();
    empty.querySelector('p').textContent = hasSearch
      ? 'No products match your search.'
      : 'No products tracked yet.';
    return;
  }

  grid.innerHTML = products.map(p => buildProductCard(p)).join('');

  products.forEach(p => {
    if (p.sparkline && p.sparkline.length > 1) {
      renderSparkline(p.id, p.sparkline);
    }
  });
}

function buildProductCard(p) {
  const savings    = p.original_price - p.current_price;
  const savingsPct = p.original_price
    ? ((savings / p.original_price) * 100).toFixed(1)
    : 0;
  const hasSavings = savings > 0.01;

  const websiteLabel = p.website === 'amazon'     ? '🟠 Amazon'
                     : p.website === 'flipkart'   ? '🔵 Flipkart'
                     : p.website === 'myntra'     ? '🩷 Myntra'
                     : p.website === 'meesho'     ? '🟣 Meesho'
                     : p.website === 'ajio'       ? '🟡 Ajio'
                     : p.website === 'nykaa'      ? '🌸 Nykaa'
                     : p.website === 'pantaloons' ? '🟤 Pantaloons'
                     : '🛒 ' + p.website;

  const siteIcon    = p.website === 'amazon'     ? '🛒'
                     : p.website === 'flipkart'   ? '🛍️'
                     : p.website === 'myntra'     ? '👗'
                     : p.website === 'meesho'     ? '🛍️'
                     : p.website === 'ajio'       ? '👔'
                     : p.website === 'nykaa'      ? '💄'
                     : p.website === 'pantaloons' ? '👕'
                     : '🛒';
  const imgHtml = p.image_url
    ? `<img class="product-img" src="${escHtml(p.image_url)}" alt="${escHtml(p.name)}" loading="lazy"
            onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : '';
  const placeholder = `<div class="product-img-placeholder"
      style="${p.image_url ? 'display:none' : ''}">
      ${siteIcon}
    </div>`;

  const savingsHtml = hasSavings
    ? `<span class="price-original">₹${formatNumber(p.original_price)}</span>
       <span class="price-savings">▼ ₹${formatNumber(savings)} (${savingsPct}% off)</span>`
    : '';

  const sparkHtml = (p.sparkline && p.sparkline.length > 1)
    ? `<div class="sparkline-wrap"><canvas id="spark-${p.id}"></canvas></div>`
    : '';

  const alertPill = p.alert_count > 0
    ? `<span class="alert-pill" title="${p.alert_count} active alert(s)">
         🔔 ${p.alert_count}
       </span>`
    : `<span class="alert-pill inactive" title="No active alerts">🔕</span>`;

  const lastChecked = p.last_checked
    ? `<small class="last-checked">Last checked: ${formatDateTime(p.last_checked)}</small>`
    : '';

  const MANUAL_SITES = ['ajio'];   // sites where auto-refresh won't work
  const isManual     = MANUAL_SITES.includes(p.website);

  return `
    <div class="product-card ${escHtml(p.website)}" id="card-${p.id}">
      <div class="card-top">
        ${imgHtml}
        ${placeholder}
        <div class="product-info">
          <p class="product-name" title="${escHtml(p.name)}">${escHtml(p.name)}</p>
          <div class="card-meta">
            <span class="website-badge ${escHtml(p.website)}">${websiteLabel}</span>
            ${alertPill}
          </div>
        </div>
      </div>

      <div class="price-section">
        <span class="price-current">₹${formatNumber(p.current_price)}</span>
        ${savingsHtml}
      </div>

      ${sparkHtml}
      ${lastChecked}

      <div class="card-actions">
        <button class="btn btn-history"
          onclick="openHistory(${p.id}, '${escAttr(p.name)}')">
          📈 History
        </button>
        ${isManual
          ? `<button class="btn btn-manual-price" title="Update price manually"
               onclick="updatePriceManual(${p.id}, this)">✏️</button>`
          : `<button class="btn btn-refresh-one" title="Refresh price"
               onclick="refreshOne(${p.id}, this)">🔄</button>`
        }
        <button class="btn btn-ghost"
          onclick="deleteProduct(${p.id})">
          🗑
        </button>
      </div>
    </div>
  `;
}

// ── Sparkline ──────────────────────────────────────────────────────────────────
function renderSparkline(productId, prices) {
  const canvas = document.getElementById(`spark-${productId}`);
  if (!canvas) return;

  const minP      = Math.min(...prices);
  const maxP      = Math.max(...prices);
  const trendDown = prices[prices.length - 1] <= prices[0];
  const lineColor = trendDown ? '#38a169' : '#e53e3e';
  const fillColor = trendDown ? 'rgba(56,161,105,.12)' : 'rgba(229,62,62,.12)';

  sparklineInstances[productId] = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels:   prices.map((_, i) => i),
      datasets: [{
        data:            prices,
        borderColor:     lineColor,
        backgroundColor: fillColor,
        borderWidth:     2,
        pointRadius:     0,
        tension:         0.35,
        fill:            true,
      }],
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      animation:           false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { display: false },
        y: { display: false, min: minP * 0.98, max: maxP * 1.02 },
      },
    },
  });
}

// ── Add product ────────────────────────────────────────────────────────────────
async function addProduct(event) {
  event.preventDefault();

  const url          = document.getElementById('productUrl').value.trim();
  const email        = document.getElementById('alertEmail').value.trim();
  const thresholdPct = parseFloat(document.getElementById('thresholdSlider').value);
  const btn          = document.getElementById('addBtn');

  if (!url || !email) { showToast('Please fill in all fields', 'error'); return; }

  btn.disabled  = true;
  btn.innerHTML = '<span class="spinner"></span> Fetching product…';

  try {
    const res  = await fetch('/api/add-product', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, email, threshold_pct: thresholdPct }),
    });
    const data = await res.json();

    if (!res.ok) {
      showToast(data.error || 'Failed to add product', 'error');
      maybeAutoSwitchManual(data.error || '');
      return;
    }

    showToast(`"${data.product?.name || 'Product'}" added successfully!`);
    document.getElementById('addForm').reset();
    document.getElementById('thresholdVal').textContent = '5';
    await Promise.all([loadStats(), loadProducts()]);

  } catch (err) {
    console.error(err);
    showToast('Network error — is the server running?', 'error');
  } finally {
    btn.disabled  = false;
    btn.innerHTML = '<span class="btn-icon">➕</span> Add Product';
  }
}

// ── Manual product add ─────────────────────────────────────────────────────────
async function addProductManual(event) {
  event.preventDefault();
  const url          = document.getElementById('manualUrl').value.trim();
  const name         = document.getElementById('manualName').value.trim();
  const price        = parseFloat(document.getElementById('manualPrice').value);
  const email        = document.getElementById('manualEmail').value.trim();
  const thresholdPct = parseFloat(document.getElementById('manualThresholdSlider').value);
  const btn          = document.getElementById('manualAddBtn');

  if (!url || !name || !email) {
    showToast('Please fill in all fields', 'error'); return;
  }
  if (isNaN(price) || price < 0) {
    showToast('Enter a valid price (0 or more)', 'error'); return;
  }

  btn.disabled  = true;
  btn.innerHTML = '<span class="spinner"></span> Adding…';

  try {
    const res  = await fetch('/api/add-product-manual', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, name, price, email, threshold_pct: thresholdPct }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || 'Failed to add', 'error'); return; }
    showToast(`"${data.product?.name}" added — update price manually via 🔄`);
    document.getElementById('manualForm').reset();
    document.getElementById('manualThresholdVal').textContent = '5';
    await Promise.all([loadStats(), loadProducts()]);
  } catch (err) {
    showToast('Network error', 'error');
  } finally {
    btn.disabled  = false;
    btn.innerHTML = '<span class="btn-icon">➕</span> Add Product Manually';
  }
}

// ── Manual price update (for bot-blocked sites like Ajio) ──────────────────────
async function updatePriceManual(productId, btnEl) {
  const newPrice = prompt('Enter the current price (₹):');
  if (newPrice === null) return;                 // user cancelled
  const price = parseFloat(newPrice.replace(/[^\d.]/g, ''));
  if (isNaN(price) || price < 0) {
    showToast('Invalid price entered', 'error'); return;
  }

  btnEl.disabled  = true;
  btnEl.innerHTML = '<span class="spinner" style="border-color:rgba(102,126,234,.4);border-top-color:var(--grad-start)"></span>';

  try {
    const res  = await fetch('/api/update-price-manual', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ product_id: productId, price }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || 'Update failed', 'error'); return; }
    showToast(`Price updated to ₹${formatNumber(price)}`);
    await Promise.all([loadStats(), loadProducts()]);
  } catch (err) {
    showToast('Network error', 'error');
  } finally {
    btnEl.disabled  = false;
    btnEl.innerHTML = '✏️';
  }
}

// ── Delete product ─────────────────────────────────────────────────────────────
async function deleteProduct(productId) {
  if (!confirm('Remove this product and all its price history?')) return;

  try {
    const res = await fetch(`/api/delete-product/${productId}`, { method: 'DELETE' });
    if (!res.ok) {
      const d = await res.json();
      showToast(d.error || 'Failed to delete product', 'error');
      return;
    }
    // Animate the card out before reloading
    const card = document.getElementById(`card-${productId}`);
    if (card) {
      card.style.transition = 'opacity .25s, transform .25s';
      card.style.opacity    = '0';
      card.style.transform  = 'scale(.95)';
      await new Promise(r => setTimeout(r, 260));
    }
    showToast('Product removed');
    await Promise.all([loadStats(), loadProducts()]);
  } catch (err) {
    console.error(err);
    showToast('Network error while deleting', 'error');
  }
}

// ── Refresh single product ─────────────────────────────────────────────────────
async function refreshOne(productId, btnEl) {
  btnEl.disabled   = true;
  btnEl.innerHTML  = '<span class="spinner" style="border-color:rgba(102,126,234,.4);border-top-color:var(--grad-start)"></span>';

  try {
    const res  = await fetch('/api/manual-check', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ product_id: productId }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || 'Refresh failed', 'error'); return; }
    showToast('Price updated');
    await Promise.all([loadStats(), loadProducts()]);
  } catch (err) {
    console.error(err);
    showToast('Network error', 'error');
  } finally {
    btnEl.disabled  = false;
    btnEl.innerHTML = '🔄';
  }
}

// ── Manual refresh all ─────────────────────────────────────────────────────────
async function manualRefresh() {
  const btn = document.getElementById('refreshBtn');
  btn.disabled  = true;
  btn.innerHTML = '<span class="spinner"></span> <span class="btn-text">Checking…</span>';

  try {
    const res  = await fetch('/api/manual-check', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || 'Refresh failed', 'error'); return; }
    showToast(`${data.message}`);
    await Promise.all([loadStats(), loadProducts(), loadSchedulerStatus()]);
  } catch (err) {
    console.error(err);
    showToast('Network error during refresh', 'error');
  } finally {
    btn.disabled  = false;
    btn.innerHTML = '<span class="btn-icon">🔄</span> <span class="btn-text">Refresh Prices</span>';
  }
}

// ── Price history modal ────────────────────────────────────────────────────────
async function openHistory(productId, productName) {
  const modal = document.getElementById('historyModal');
  document.getElementById('modalTitle').textContent   = `📈 ${productName}`;
  document.getElementById('historyStats').innerHTML   = '';
  document.getElementById('alertsSection').innerHTML  =
    '<p class="loading-text">Loading alerts…</p>';
  modal.classList.add('open');

  // Destroy previous chart
  if (historyChartInstance) {
    historyChartInstance.destroy();
    historyChartInstance = null;
  }
  // Reset canvas (create fresh element to avoid Chart.js reuse error)
  const oldCanvas = document.getElementById('historyChart');
  const newCanvas = document.createElement('canvas');
  newCanvas.id     = 'historyChart';
  newCanvas.height = 300;
  oldCanvas.replaceWith(newCanvas);

  // Fetch history and alerts in parallel
  const [histRes, alertRes] = await Promise.all([
    fetch(`/api/product-history/${productId}`).catch(() => null),
    fetch(`/api/alerts/${productId}`).catch(() => null),
  ]);

  // ── Render chart ──
  if (histRes && histRes.ok) {
    const data    = await histRes.json();
    const history = data.history || [];

    if (history.length) {
      const prices = history.map(h => h.price);
      const labels = history.map(h => formatDateTime(h.checked_at));
      const minP   = Math.min(...prices);
      const maxP   = Math.max(...prices);
      const latest = prices[prices.length - 1];

      historyChartInstance = new Chart(
        document.getElementById('historyChart').getContext('2d'),
        {
          type: 'line',
          data: {
            labels,
            datasets: [{
              label:                'Price (₹)',
              data:                 prices,
              borderColor:          '#667eea',
              backgroundColor:      'rgba(102,126,234,.1)',
              borderWidth:          2.5,
              pointRadius:          history.length < 20 ? 4 : 2,
              pointBackgroundColor: '#667eea',
              tension:              0.3,
              fill:                 true,
            }],
          },
          options: {
            responsive: true,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: { label: ctx => ` ₹${formatNumber(ctx.parsed.y)}` },
              },
            },
            scales: {
              x: { ticks: { maxTicksLimit: 8, maxRotation: 45, font: { size: 11 } } },
              y: { ticks: { callback: v => '₹' + formatNumber(v), font: { size: 11 } } },
            },
          },
        }
      );

      const range = maxP - minP;
      document.getElementById('historyStats').innerHTML = `
        <div class="history-stat">
          <span class="history-stat-value">₹${formatNumber(latest)}</span>
          <span class="history-stat-label">Current Price</span>
        </div>
        <div class="history-stat">
          <span class="history-stat-value">₹${formatNumber(minP)}</span>
          <span class="history-stat-label">All-time Low</span>
        </div>
        <div class="history-stat">
          <span class="history-stat-value">${history.length}</span>
          <span class="history-stat-label">Data Points</span>
        </div>
        <div class="history-stat">
          <span class="history-stat-value">₹${formatNumber(range)}</span>
          <span class="history-stat-label">Price Range</span>
        </div>
      `;
    } else {
      document.getElementById('historyStats').innerHTML =
        '<p class="loading-text">No price history recorded yet.</p>';
    }
  } else {
    document.getElementById('historyStats').innerHTML =
      '<p style="color:var(--danger)">Failed to load history.</p>';
  }

  // ── Render alerts ──
  if (alertRes && alertRes.ok) {
    const alertData = await alertRes.json();
    renderAlertsInModal(alertData.alerts || [], productId);
  } else {
    document.getElementById('alertsSection').innerHTML =
      '<p style="color:var(--danger)">Failed to load alerts.</p>';
  }
}

function renderAlertsInModal(alerts, productId) {
  const el = document.getElementById('alertsSection');
  if (!alerts.length) {
    el.innerHTML = '<p class="loading-text">No alerts configured for this product.</p>';
    return;
  }

  const rows = alerts.map(a => {
    const status = a.is_active
      ? '<span class="alert-status active">● Active</span>'
      : '<span class="alert-status fired">✓ Fired</span>';
    const reactivateBtn = !a.is_active
      ? `<button class="btn-inline btn-reactivate"
           onclick="reactivateAlert(${a.id}, ${productId})">↺ Re-enable</button>`
      : '';
    return `
      <div class="alert-row">
        <div class="alert-row-info">
          ${status}
          <span class="alert-email">${escHtml(a.email)}</span>
          <span class="alert-threshold">when ≥ ${a.threshold_pct}% drop</span>
        </div>
        <div class="alert-row-actions">
          ${reactivateBtn}
          <button class="btn-inline btn-del-alert"
            onclick="deleteAlert(${a.id}, ${productId})">✕</button>
        </div>
      </div>
    `;
  }).join('');

  el.innerHTML = `<h4 class="alerts-title">🔔 Alerts</h4>${rows}`;
}

async function reactivateAlert(alertId, productId) {
  try {
    const res = await fetch(`/api/alerts/${alertId}/reactivate`, { method: 'POST' });
    if (!res.ok) { showToast('Failed to reactivate alert', 'error'); return; }
    showToast('Alert re-enabled');
    // Reload alert section only (no need to close the modal)
    const alertRes = await fetch(`/api/alerts/${productId}`);
    if (alertRes.ok) {
      const data = await alertRes.json();
      renderAlertsInModal(data.alerts || [], productId);
    }
    loadStats(); // active alert count may have changed
  } catch (err) {
    console.error(err);
    showToast('Network error', 'error');
  }
}

async function deleteAlert(alertId, productId) {
  if (!confirm('Delete this alert?')) return;
  try {
    const res = await fetch(`/api/alerts/${alertId}`, { method: 'DELETE' });
    if (!res.ok) { showToast('Failed to delete alert', 'error'); return; }
    showToast('Alert deleted');
    const alertRes = await fetch(`/api/alerts/${productId}`);
    if (alertRes.ok) {
      const data = await alertRes.json();
      renderAlertsInModal(data.alerts || [], productId);
    }
    await Promise.all([loadStats(), loadProducts()]);
  } catch (err) {
    console.error(err);
    showToast('Network error', 'error');
  }
}

// ── Modal close ────────────────────────────────────────────────────────────────
function closeModal(event) {
  const overlay = document.getElementById('historyModal');
  // Close when: called directly (no event), or clicking the dark overlay background
  if (!event || event.target === overlay) {
    overlay.classList.remove('open');
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('historyModal').classList.remove('open');
});

// ── Search / filter ────────────────────────────────────────────────────────────
// Debounce so we don't re-render on every keystroke
let searchTimer = null;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilter, 150);
}

// ── Toast notifications ────────────────────────────────────────────────────────
function showToast(message, type = 'success', duration = 4000) {
  const container = document.getElementById('toastContainer');
  const icons     = { success: '✅', error: '❌', warning: '⚠️' };
  const toast     = document.createElement('div');
  toast.className = `toast ${type !== 'success' ? type : ''}`;
  toast.innerHTML =
    `<span>${icons[type] || '✅'}</span><span>${escHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.animation = 'toastOut .3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function updateProductCount(total, shown) {
  const el = document.getElementById('productCount');
  if (total === shown || shown === undefined) {
    el.textContent = total === 1 ? '1 product' : `${total} products`;
  } else {
    el.textContent = `${shown} of ${total} products`;
  }
}

function formatNumber(n) {
  if (n == null || isNaN(n)) return '0';
  return Number(n).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatDateTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
    return d.toLocaleString('en-IN', {
      day:    '2-digit',
      month:  'short',
      hour:   '2-digit',
      minute: '2-digit',
      hour12: true,
    });
  } catch {
    return iso;
  }
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escAttr(str) {
  return String(str ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}
