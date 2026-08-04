(() => {
  'use strict';

  const body = document.body;
  const role = body.dataset.role;
  const owner = role === 'owner';
  let locationName = body.dataset.location || 'shenzhen';
  const view = document.querySelector('#app-view');
  const loading = document.querySelector('#loading');
  const modalRoot = document.querySelector('#modal-root');
  const toast = document.querySelector('.toast');
  let toastTimer;
  let state = { menu: null, dishes: [], categories: [], context: null };

  const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const pair = (cn, en, tag = 'span') => `<${tag} class="bilingual-pair"><span class="lang-zh">${esc(cn)}</span><span class="lang-en">${esc(en || '')}</span></${tag}>`;
  const jsonList = value => { try { return Array.isArray(value) ? value : JSON.parse(value || '[]'); } catch (_) { return []; } };
  const image = dish => dish.image ? `/photos/${encodeURIComponent(dish.image)}` : '';

  function splitHistoricalCombo(item) {
    if (!item?.is_historical_combo) return [item];
    return String(item.name_cn || item.custom_name || '')
      .replaceAll('＋', '+')
      .split('+')
      .map(name => name.trim())
      .filter(Boolean)
      .map(name => {
        const primary = name.split(/\s+\/\s+/)[0].trim();
        const exact = state.dishes.find(d => d.name_cn === primary);
        const nearby = exact || state.dishes.find(d => d.name_cn.includes(primary) || primary.includes(d.name_cn));
        return {
          ...item,
          dish_id: nearby?.id || '',
          name_cn: name,
          name_en: nearby?.name_en || '',
          image: nearby?.image || '',
          meal_roles: nearby?.meal_roles || [],
          custom_tags: nearby?.custom_tags || [],
          source: 'historical',
          virtual_historical_item: true,
        };
      });
  }

  function displayItems(items) {
    return (items || []).flatMap(splitHistoricalCombo);
  }

  function notify(message, isError = false) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.add('show');
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    return data;
  }

  async function post(path, payload) {
    return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  }

  function currentRoute() {
    const path = location.pathname.replace(/^\//, '');
    return ['tomorrow', 'pantry', 'dishes', 'history'].includes(path) ? path : (owner ? 'tomorrow' : 'pantry');
  }

  function setChrome() {
    const labels = {shenzhen: '深圳 Shenzhen', hongkong: '香港 Hong Kong'};
    document.querySelector('#current-kitchen').textContent = labels[locationName];
    document.querySelector('#role-badge').textContent = owner ? 'Owner' : 'Worker';
    document.querySelectorAll('.kitchen-button').forEach(button => {
      const active = button.dataset.location === locationName;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
      button.onclick = () => {
        document.cookie = `loc=${button.dataset.location}; Path=/; SameSite=Lax; Max-Age=31536000`;
        location.reload();
      };
    });
    const route = currentRoute();
    document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.route === route));
    if (!owner) document.querySelector('[data-route="tomorrow"]').hidden = true;
  }

  function heading(titleCn, titleEn, description, side = '') {
    return `<section class="view-heading"><div>${pair(titleCn, titleEn, 'h1')}<p>${esc(description)}</p></div>${side}</section>`;
  }

  function errorView(error) {
    view.innerHTML = `<section class="error-state"><h1>加载失败</h1><p>${esc(error.message)}</p><button class="primary-button" id="retry">重新加载 Retry</button></section>`;
    document.querySelector('#retry').onclick = loadRoute;
  }

  function dishCard(item, editable = false) {
    const src = image(item);
    const tags = [...jsonList(item.meal_roles), ...jsonList(item.custom_tags)].slice(0, 2);
    return `<article class="dish-card" data-dish-id="${esc(item.dish_id || item.id)}">
      ${src ? `<img src="${src}" alt="${esc(item.name_cn)}" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false">` : ''}
      <div class="no-img" ${src ? 'hidden' : ''}>暂无图片<br><small>No image</small></div>
      <div class="dish-copy">${item.source === 'ai' ? '<div class="ai-label">智能推荐 AI</div>' : ''}${pair(item.name_cn, item.name_en, 'h3')}<div class="tags">${tags.map(tag => `<span>${esc(tag)}</span>`).join('')}</div></div>
      ${editable ? `<div class="dish-actions"><button class="swap-button" data-action="replace" data-item="${item.menu_item_id}">更换 Replace</button><button class="remove-button" data-action="remove" data-item="${item.menu_item_id}">移除 Remove</button></div>` : ''}
    </article>`;
  }

  async function renderTomorrow() {
    const [menu, context, dishes] = await Promise.all([api('/api/tomorrow'), api('/api/ui-context'), api('/api/dishes')]);
    state.menu = menu; state.context = context; state.dishes = dishes;
    const statusLabel = {draft: '待确认 Pending', confirmed: '已确认 Confirmed', pushed: '已推送 Pushed'}[menu.status] || menu.status;
    const diners = context.diners.map(d => `<button class="person ${context.selected_diners.includes(d.id) ? 'selected' : ''}" data-diner="${esc(d.id)}" aria-pressed="${context.selected_diners.includes(d.id)}"><span class="person-avatar">${esc((d.name_en || d.name_cn).slice(0,1))}</span>${pair(d.name_cn, d.name_en)}</button>`).join('');
    const mealLabels = {breakfast:['早餐','Breakfast'], lunch:['午餐','Lunch'], afternoon_snack:['下午茶','Afternoon Tea'], dinner:['晚餐','Dinner']};
    const meals = Object.entries(mealLabels).map(([key, labels]) => {
      const rawItems = menu.meals?.[key] || [];
      const items = displayItems(rawItems);
      const accent = key === 'breakfast' ? 'amber' : key === 'lunch' ? 'blue' : key === 'afternoon_snack' ? 'green' : 'red';
      const controls = owner ? `<div class="meal-actions"><button class="text-button add-button" data-action="add" data-meal="${key}">添加 Add</button>${key === 'afternoon_snack' ? '' : `<button class="text-button fill-button" data-action="fill" data-meal="${key}">智能补充 AI Fill</button>`}</div>` : '';
      return `<section class="meal-section ${key === 'afternoon_snack' ? 'optional' : ''}" data-meal="${key}"><header class="meal-header"><div class="meal-title"><span class="meal-accent ${accent}"></span><div>${pair(labels[0], labels[1], 'h2')}<p>${items.length ? `${items.length} 道 dishes` : '可选 Optional'}</p></div></div>${controls}</header><div class="dish-grid">${items.length ? items.map(item => dishCard(item, owner && !item.virtual_historical_item)).join('') : '<div class="empty-state"><div class="empty-icon">+</div><div><strong>暂无安排 No dishes</strong><p>需要时可添加菜品。</p></div></div>'}</div></section>`;
    }).join('');
    const warnings = [...(context.warnings || [])];
    if (menu.review_issues) warnings.push(menu.review_issues);
    const banquetTotal = context.banquet_total_diners || 8;
    view.innerHTML = `<section class="page-heading"><div><p class="eyebrow">${esc(menu.date)}</p><h1>明日菜单</h1><p>Tomorrow menu for ${esc(context.location_label)}</p></div><div class="status-chip"><span></span>${esc(statusLabel)}</div></section>
      <div class="desktop-layout"><aside class="planner-panel"><section class="settings-block"><div class="section-label"><span>用餐成员 Diners</span><small>${context.selected_diners.length} 人</small></div><div class="people-grid">${diners}</div></section><section class="settings-block compact ${context.meal_mode === 'banquet' ? 'banquet-active' : ''}"><div class="section-label"><span>用餐模式 Meal mode</span></div><div class="segmented"><button data-mode="daily" class="${context.meal_mode === 'daily' ? 'selected' : ''}">日常 Daily</button><button data-mode="banquet" class="${context.meal_mode === 'banquet' ? 'selected' : ''}">家宴 Banquet</button></div>${context.meal_mode === 'banquet' ? `<div class="banquet-count"><span>家宴总人数 Banquet diners</span><div class="stepper"><button data-banquet-step="-1">−</button><strong id="banquet-total">${banquetTotal}</strong><button data-banquet-step="1">+</button></div></div>` : ''}</section>${warnings.length ? `<section class="nutrition-card"><div class="nutrition-heading"><div><small>菜单提示 Review</small><h2>${warnings.length} 项需要留意</h2></div></div>${warnings.map(w => `<p>${esc(w)}</p>`).join('')}</section>` : ''}${owner ? `<div class="desktop-confirm"><button class="secondary-button" data-action="repair">重新生成 Regenerate</button><button class="primary-button" data-action="confirm">确认菜单 Confirm</button></div>` : ''}</aside><div class="menu-content">${meals}</div></div>${owner ? `<div class="mobile-action-bar"><button class="secondary-button" data-action="repair">重新生成</button><button class="primary-button" data-action="confirm">确认菜单</button></div>` : ''}`;
    bindTomorrow();
  }

  function bindTomorrow() {
    if (!owner) return;
    document.querySelectorAll('[data-diner]').forEach(button => button.onclick = async () => {
      const selected = [...document.querySelectorAll('[data-diner].selected')].map(x => x.dataset.diner);
      const id = button.dataset.diner; const next = selected.includes(id) ? selected.filter(x => x !== id) : [...selected, id];
      if (!next.length) return notify('至少选择一位用餐成员', true);
      await post('/api/tomorrow/diners', {menu_id: state.menu.menu_id, diners: next, location: locationName}); await renderTomorrow();
    });
    document.querySelectorAll('[data-mode]').forEach(button => button.onclick = async () => { await post('/api/tomorrow/meal-mode', {menu_id: state.menu.menu_id, meal_mode: button.dataset.mode, banquet_total_diners: button.dataset.mode === 'banquet' ? 8 : null, location: locationName}); await renderTomorrow(); });
    document.querySelectorAll('[data-banquet-step]').forEach(button => button.onclick = async () => {
      const current = Number(document.querySelector('#banquet-total').textContent);
      const total = Math.max(1, Math.min(30, current + Number(button.dataset.banquetStep)));
      await post('/api/tomorrow/meal-mode', {menu_id: state.menu.menu_id, meal_mode: 'banquet', banquet_total_diners: total, location: locationName});
      await renderTomorrow();
    });
    document.querySelectorAll('[data-action]').forEach(button => button.onclick = () => menuAction(button));
  }

  async function menuAction(button) {
    const action = button.dataset.action;
    try {
      if (action === 'add' || action === 'replace') return openDishPicker(action, button.dataset.meal, button.dataset.item);
      if (action === 'remove') { if (!confirm('确定移除这道菜吗？')) return; await post('/api/tomorrow/remove', {menu_id: state.menu.menu_id, menu_item_id: Number(button.dataset.item)}); }
      if (action === 'fill') await post('/api/tomorrow/ai-fill', {menu_id: state.menu.menu_id, location: locationName, meal_type: button.dataset.meal});
      if (action === 'repair') { if (!confirm('确定重新生成未锁定的菜单吗？')) return; await post('/api/tomorrow/repair', {menu_id: state.menu.menu_id, location: locationName}); }
      if (action === 'confirm') { if (!confirm('确认明日菜单？当前不会发送 PushPlus。')) return; await post('/api/tomorrow/confirm', {menu_id: state.menu.menu_id}); }
      notify('已保存 Saved'); await renderTomorrow();
    } catch (error) { notify(error.message, true); }
  }

  async function openDishPicker(mode, meal, itemId) {
    const dishes = state.dishes.length ? state.dishes : await api('/api/dishes'); state.dishes = dishes;
    modalRoot.innerHTML = `<div class="picker-backdrop"><section class="picker" role="dialog" aria-modal="true"><header><div><h2>${mode === 'replace' ? '更换菜品 Replace' : '添加菜品 Add'}</h2><p>搜索真实菜品库</p></div><button class="picker-close">×</button></header><input class="picker-search" placeholder="搜索菜名 Search dishes"><div class="picker-results"></div><footer><button class="secondary-button picker-close">取消 Cancel</button></footer></section></div>`;
    const render = q => { const matches = dishes.filter(d => `${d.name_cn} ${d.name_en || ''}`.toLowerCase().includes(q.toLowerCase())).slice(0, 50); modalRoot.querySelector('.picker-results').innerHTML = matches.map(d => `<button class="picker-item" data-id="${esc(d.id)}">${image(d) ? `<img src="${image(d)}" alt="">` : '<div class="no-img">No image</div>'}<span>${pair(d.name_cn, d.name_en, 'strong')}</span></button>`).join(''); modalRoot.querySelectorAll('.picker-item').forEach(button => button.onclick = async () => { try { if (mode === 'replace') await post('/api/tomorrow/replace', {menu_id: state.menu.menu_id, menu_item_id: Number(itemId), new_dish_id: button.dataset.id}); else await post('/api/tomorrow/add', {menu_id: state.menu.menu_id, dish_id: button.dataset.id, meal_type: meal}); closeModal(); notify('已保存 Saved'); await renderTomorrow(); } catch (error) { notify(error.message, true); } }); };
    modalRoot.querySelector('.picker-search').oninput = event => render(event.target.value); modalRoot.querySelectorAll('.picker-close').forEach(x => x.onclick = closeModal); render('');
  }

  async function renderPantry() {
    const [pantry, ingredients, common] = await Promise.all([api('/api/pantry'), api('/api/ingredients'), api('/api/pantry/last')]);
    const items = pantry.items || [];
    view.innerHTML = `${heading('食材库存','Pantry', `当前厨房：${pantry.location || locationName}`, `<div class="view-count">${items.length}<small>项</small></div>`)}<div class="pantry-toolbar"><label>搜索或添加食材 Search<input id="pantry-search" autocomplete="off"><span id="pantry-search-feedback"></span></label><button class="primary-button" id="same-as-last">和上次一样 Same as last</button></div><div class="pantry-layout"><section class="inventory-panel"><header><div>当前库存<small>Current pantry</small></div></header><div id="inventory-list">${items.map(pantryRow).join('')}</div></section><aside class="pantry-aside"><section><h2>常用食材 Common</h2><p>点击加入当前库存</p><div class="common-chips">${ingredients.slice(0, 12).map(i => `<button data-add-ing="${esc(i.ingredient_id)}">${pair(i.name_cn, i.name_en)}</button>`).join('')}</div></section></aside></div>`;
    document.querySelector('#same-as-last').onclick = () => pantryPost('/api/pantry/same-as-last', {location: locationName});
    document.querySelectorAll('[data-status]').forEach(button => button.onclick = () => pantryPost('/api/pantry/update_status', {ingredient_id: button.dataset.id, status: button.dataset.status, location: locationName}));
    document.querySelectorAll('[data-remove-ing]').forEach(button => button.onclick = () => { if (confirm('确定已经用完？')) pantryPost('/api/pantry/remove', {ingredient_id: button.dataset.removeIng, location: locationName}); });
    document.querySelectorAll('[data-add-ing]').forEach(button => button.onclick = () => pantryPost('/api/pantry/add', {ingredient_id: button.dataset.addIng, location: locationName}));
    const search = document.querySelector('#pantry-search'); search.oninput = () => { const q = search.value.trim().toLowerCase(); const match = ingredients.find(i => `${i.name_cn} ${i.name_en || ''}`.toLowerCase().includes(q)); document.querySelector('#pantry-search-feedback').innerHTML = q && match ? `<button data-found="${esc(match.ingredient_id)}">加入 ${esc(match.name_cn)}</button>` : q ? '没有匹配食材 No match' : ''; const found = document.querySelector('[data-found]'); if (found) found.onclick = () => pantryPost('/api/pantry/add', {ingredient_id: found.dataset.found, location: locationName}); };
  }

  function pantryRow(item) { return `<div class="ingredient-row" data-state="${esc(item.status)}"><div>${pair(item.name_cn, item.name_en, 'strong')}</div><div class="stock-actions"><button class="${item.status === 'priority_use' ? 'selected' : ''}" data-status="priority_use" data-id="${esc(item.ingredient_id)}">优先用<br>Use first</button><button class="${item.status === 'expiring' ? 'selected soon' : ''}" data-status="expiring" data-id="${esc(item.ingredient_id)}">快过期<br>Expiring</button><button class="used" data-remove-ing="${esc(item.ingredient_id)}">用完<br>Used up</button></div></div>`; }
  async function pantryPost(path, payload) { try { await post(path, payload); notify('已保存 Saved'); await renderPantry(); } catch (error) { notify(error.message, true); } }

  async function renderDishes() {
    const [dishes, categories] = await Promise.all([api('/api/dishes'), api('/api/categories')]); state.dishes = dishes; state.categories = categories;
    view.innerHTML = `${heading('菜品库','Dishes','浏览当前启用的真实菜品', `<div class="view-count">${dishes.length}<small>道</small></div>`)}<div class="dish-toolbar"><input id="dish-search" placeholder="搜索菜名 Search dishes"><div class="filter-row"><button class="active" data-category="">全部 All</button>${categories.map(c => `<button data-category="${esc(c.id)}">${esc(c.label_cn)}</button>`).join('')}</div></div><div class="library-grid" id="dish-library"></div>`;
    let category = ''; const draw = () => { const q = document.querySelector('#dish-search').value.toLowerCase(); const filtered = dishes.filter(d => (!category || d.category_id === category) && `${d.name_cn} ${d.name_en || ''}`.toLowerCase().includes(q)); document.querySelector('#dish-library').innerHTML = filtered.map((d, index) => `<button class="library-card" data-index="${dishes.indexOf(d)}">${image(d) ? `<img src="${image(d)}" alt="${esc(d.name_cn)}" loading="lazy">` : '<div class="no-img">No image</div>'}<span class="library-copy">${pair(d.name_cn, d.name_en, 'strong')}<small>${esc(categories.find(c => c.id === d.category_id)?.label_cn || '')}</small></span></button>`).join(''); document.querySelectorAll('.library-card').forEach(button => button.onclick = () => showDish(dishes[Number(button.dataset.index)])); };
    document.querySelector('#dish-search').oninput = draw; document.querySelectorAll('[data-category]').forEach(button => button.onclick = () => { document.querySelectorAll('[data-category]').forEach(x => x.classList.remove('active')); button.classList.add('active'); category = button.dataset.category; draw(); }); draw();
  }

  function showDish(dish) { modalRoot.innerHTML = `<div class="detail-backdrop"><section class="dish-detail" role="dialog" aria-modal="true"><button class="detail-close">×</button>${image(dish) ? `<img src="${image(dish)}" alt="${esc(dish.name_cn)}">` : '<div class="no-img">No image</div>'}<div>${pair(dish.name_cn, dish.name_en, 'h2')}<span class="detail-category">${esc(state.categories.find(c => c.id === dish.category_id)?.label_cn || '')}</span></div>${owner ? `<footer>${[['breakfast','早餐'],['lunch','午餐'],['dinner','晚餐']].map(([key,label]) => `<button class="text-button" data-add-meal="${key}">加入${label}</button>`).join('')}</footer>` : ''}</section></div>`; document.querySelector('.detail-close').onclick = closeModal; document.querySelectorAll('[data-add-meal]').forEach(button => button.onclick = async () => { try { const menu = state.menu || await api('/api/tomorrow'); await post('/api/tomorrow/add', {menu_id: menu.menu_id, dish_id: dish.id, meal_type: button.dataset.addMeal}); closeModal(); notify('已加入菜单'); } catch (error) { notify(error.message, true); } }); }
  function closeModal() { modalRoot.innerHTML = ''; }

  async function renderHistory() {
    const menus = await api('/api/history');
    view.innerHTML = `${heading('历史菜单','History','查看 SQLite 中保存的历史菜单', `<div class="view-count"><b class="count-number">${menus.length}</b><small>天 days</small></div>`)}<div class="history-list">${menus.map(menu => `<article class="history-card"><header><div><h2>${esc(menu.date)}</h2><small>${esc(menu.location)}</small></div><span class="history-status ${menu.status === 'confirmed' ? 'confirmed' : ''}">${esc(menu.status)}</span></header><div class="history-meals">${[['breakfast','早餐'],['lunch','午餐'],['afternoon_snack','下午茶'],['dinner','晚餐']].map(([key,label]) => `<div><b>${label}</b><p>${(menu.meals?.[key] || []).map(d => `<span class="history-dish">${image(d) ? `<img src="${image(d)}" alt="" loading="lazy" onerror="this.remove()">` : '<span class="history-placeholder" aria-hidden="true"></span>'}<span>${esc(d.name_cn)}${d.name_en ? `<small>${esc(d.name_en)}</small>` : ''}</span></span>`).join('') || '未安排'}</p></div>`).join('')}</div></article>`).join('')}</div>`;
  }

  async function loadRoute() {
    loading.hidden = false; view.innerHTML = '';
    try { const route = currentRoute(); if (route === 'tomorrow') await renderTomorrow(); else if (route === 'pantry') await renderPantry(); else if (route === 'dishes') await renderDishes(); else await renderHistory(); }
    catch (error) { errorView(error); }
    finally { loading.hidden = true; }
  }

  setChrome(); loadRoute();
})();
