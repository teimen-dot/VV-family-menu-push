(() => {
  'use strict';

  const body = document.body;
  const root = document.querySelector('#menuRoot');
  const dayNav = document.querySelector('#dayNav');
  const kitchenName = document.querySelector('#kName');
  const initialLocation = body.dataset.location || 'shenzhen';

  const MEALS = {
    breakfast: {cn: '早餐', en: 'Breakfast', icon: '☀'},
    lunch: {cn: '午餐', en: 'Lunch', icon: '◉'},
    dinner: {cn: '晚餐', en: 'Dinner', icon: '☾'},
  };
  const STATUS = {
    draft: ['待确认', 'Pending'],
    confirmed: ['已确认', 'Confirmed'],
    pushed: ['已推送', 'Pushed'],
    not_generated: ['尚未生成', 'Not generated'],
  };
  const AVAILABILITY = {
    available: ['库存可做', 'AVAILABLE'],
    almost_available: ['差少量', 'ALMOST'],
    missing: ['缺食材', 'MISSING'],
    incomplete: ['食材待完善', 'INCOMPLETE'],
    unknown: ['库存未知', 'UNKNOWN'],
  };

  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);

  function photoUrl(image) {
    if (!image) return '';
    return `/photos/${String(image).split('/').map(encodeURIComponent).join('/')}`;
  }

  function statusMarkup(status) {
    const labels = STATUS[status] || [status || '未知', 'Unknown'];
    const extra = status === 'not_generated' ? ' not-generated' : '';
    return `<span class="status${extra}">${escapeHtml(labels[0])} · ${escapeHtml(labels[1])}</span>`;
  }

  function availabilityFor(menu, dish) {
    const result = menu?.availability?.[dish.dish_id] || {};
    const status = AVAILABILITY[result.status] ? result.status : 'unknown';
    const labels = AVAILABILITY[status];
    const missing = (result.missing_required || []).map(item => item.name_cn).filter(Boolean);
    return {
      status,
      label: `${labels[0]} · ${labels[1]}`,
      detail: missing.length ? `缺 ${missing.join('、')}` : '',
    };
  }

  function dishMarkup(dish, menu) {
    const availability = availabilityFor(menu, dish);
    const image = photoUrl(dish.image);
    const nameCn = dish.name_cn || dish.custom_name || dish.dish_id || '未命名菜品';
    const nameEn = dish.name_en || '';
    return `<article class="dish-row"
      data-menu-id="${escapeHtml(menu.menu_id)}"
      data-menu-item-id="${escapeHtml(dish.menu_item_id)}"
      data-dish-id="${escapeHtml(dish.dish_id)}">
      ${image
        ? `<img class="dish-photo" src="${image}" alt="${escapeHtml(nameCn)}" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="dish-placeholder" hidden aria-hidden="true">🍽</span>`
        : '<span class="dish-placeholder" aria-hidden="true">🍽</span>'}
      <div class="dish-info">
        <h3>${escapeHtml(nameCn)}</h3>
        ${nameEn ? `<small>${escapeHtml(nameEn)}</small>` : ''}
        <div class="dish-sub">
          <span class="availability ${availability.status}" title="${escapeHtml(availability.detail)}">${escapeHtml(availability.label)}</span>
          <span class="record-ids">dish ${escapeHtml(dish.dish_id || '—')} · item ${escapeHtml(dish.menu_item_id || '—')}</span>
        </div>
      </div>
    </article>`;
  }

  function emptyMealMarkup(menu) {
    const message = menu.exists ? ['本餐暂无菜品', 'NO DISHES'] : ['当天餐单尚未生成', 'MENU NOT GENERATED'];
    return `<div class="empty-meal">${message[0]}<small>${message[1]}</small></div>`;
  }

  function mealCardMarkup(day, mealType) {
    const menu = day.menu;
    const meta = MEALS[mealType];
    const dishes = menu.meals?.[mealType] || [];
    const note = menu.meal_notes?.[mealType] || '';
    return `<article class="card meal-card ${mealType}" data-meal-type="${mealType}" data-menu-id="${escapeHtml(menu.menu_id)}">
      <button class="meal-head" type="button" aria-expanded="true">
        <span class="meal-icon" aria-hidden="true">${meta.icon}</span>
        <span class="meal-title"><strong>${meta.cn}</strong><small>${meta.en}</small></span>
        <span class="meal-count">${dishes.length} 道</span>
        <span class="chev" aria-hidden="true">⌃</span>
      </button>
      <div class="meal-detail">
        <div class="meal-meta">
          ${statusMarkup(menu.status)}
          <span>${menu.diners_count == null ? '人数未设置' : `${escapeHtml(menu.diners_count)} 人`} · DINERS</span>
          <span class="menu-id">menu ${escapeHtml(menu.menu_id || '—')}</span>
        </div>
        ${dishes.length ? dishes.map(dish => dishMarkup(dish, menu)).join('') : emptyMealMarkup(menu)}
        ${note ? `<p class="meal-note">📝 ${escapeHtml(note)}</p>` : ''}
      </div>
    </article>`;
  }

  function nextMealMarkup(data) {
    const next = data.next_meal;
    if (!next) {
      return `<section class="card next-card"><div class="next-side"><p class="next-kicker">NEXT MEAL</p><h1>暂无下一餐</h1></div><div class="next-detail"><div class="empty-meal">四日内没有可显示的餐次<small>NO UPCOMING MEAL</small></div></div></section>`;
    }
    const meal = MEALS[next.meal_type];
    const syntheticMenu = {
      menu_id: next.menu_id,
      availability: next.availability || {},
    };
    return `<section class="card next-card" id="nextMeal" data-date="${escapeHtml(next.date)}" data-meal-type="${escapeHtml(next.meal_type)}" data-menu-id="${escapeHtml(next.menu_id)}">
      <div class="next-side">
        <p class="next-kicker">下一餐 · NEXT MEAL</p>
        <h1>${escapeHtml(meal.cn)}</h1>
        <div class="next-meta">
          <span>${escapeHtml(next.day_label_cn)} · ${escapeHtml(next.day_label_en)}</span>
          <span>${escapeHtml(next.date)}</span>
          <span>${next.diners_count == null ? '人数未设置' : `${escapeHtml(next.diners_count)} 人`} · DINERS</span>
          <span>${statusMarkup(next.status)}</span>
        </div>
        <div class="next-menu-id">menu ${escapeHtml(next.menu_id || '—')}</div>
      </div>
      <div class="next-detail">
        ${next.dishes?.length ? next.dishes.map(dish => dishMarkup(dish, syntheticMenu)).join('') : '<div class="empty-meal">本餐暂无真实菜品<small>NO LIVE DISHES</small></div>'}
        ${next.note ? `<p class="meal-note">📝 ${escapeHtml(next.note)}</p>` : ''}
      </div>
    </section>`;
  }

  function dayMarkup(day) {
    const date = new Date(`${day.date}T00:00:00`);
    const dateLabel = `${date.getMonth() + 1}月${date.getDate()}日`;
    return `<section class="day-sec" id="day-${day.offset}" data-date="${escapeHtml(day.date)}" data-menu-id="${escapeHtml(day.menu.menu_id)}">
      <header class="sec-head">
        <strong>${escapeHtml(day.label_cn)} · ${escapeHtml(dateLabel)}</strong>
        <span>${escapeHtml(day.label_en)} · ${escapeHtml(day.weekday_en)}</span>
        <small>${escapeHtml(day.weekday_cn)}</small>
      </header>
      <div class="meal-grid">
        ${Object.keys(MEALS).map(mealType => mealCardMarkup(day, mealType)).join('')}
      </div>
    </section>`;
  }

  function renderDayNav(days) {
    dayNav.innerHTML = days.map(day => {
      const date = new Date(`${day.date}T00:00:00`);
      return `<button type="button" data-target="day-${day.offset}" class="${day.offset === 0 ? 'on' : ''}">
        <strong>${escapeHtml(day.label_cn)}</strong>
        <small>${date.getMonth() + 1}/${date.getDate()} · ${escapeHtml(day.label_en)}</small>
      </button>`;
    }).join('');
    dayNav.querySelectorAll('button').forEach(button => {
      button.addEventListener('click', () => {
        document.querySelector(`#${button.dataset.target}`)?.scrollIntoView({behavior: 'smooth', block: 'start'});
      });
    });
  }

  function bindReadOnlyInteractions() {
    document.querySelectorAll('.meal-head').forEach(button => {
      button.addEventListener('click', () => {
        const card = button.closest('.meal-card');
        const closed = card.classList.toggle('closed');
        button.setAttribute('aria-expanded', String(!closed));
      });
    });

    const sections = [...document.querySelectorAll('.day-sec')];
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      dayNav.querySelectorAll('button').forEach(button => {
        button.classList.toggle('on', button.dataset.target === visible.target.id);
      });
    }, {rootMargin: '-145px 0px -55% 0px', threshold: [0, .2, .6]});
    sections.forEach(section => observer.observe(section));
  }

  function setKitchenChrome(data) {
    kitchenName.textContent = data.location_label;
    document.querySelector('.k-banner').classList.toggle('hk', data.location === 'hongkong');
    document.querySelectorAll('#kSwitch .k-btn').forEach(button => {
      const active = button.dataset.location === data.location;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
      button.addEventListener('click', () => {
        if (button.dataset.location === data.location) return;
        document.cookie = `loc=${button.dataset.location}; Path=/; SameSite=Lax; Max-Age=31536000`;
        window.location.reload();
      });
    });
  }

  function render(data) {
    setKitchenChrome(data);
    renderDayNav(data.days);
    root.innerHTML = `${nextMealMarkup(data)}${data.days.map(dayMarkup).join('')}
      <p class="readonly-note">第一阶段为真实数据只读视图；确认、取消、人数、备注、换菜、增删、智能补充和重新生成均未开放。<br><small>LIVE DATA · READ ONLY · NO MENU MUTATIONS</small></p>`;
    bindReadOnlyInteractions();
  }

  function renderError(error) {
    root.innerHTML = `<section class="card error-card"><h1>餐单加载失败</h1><p>${escapeHtml(error.message)}</p><button class="retry" type="button">重试 Retry</button></section>`;
    root.querySelector('.retry').addEventListener('click', load);
  }

  async function load() {
    root.innerHTML = '<section class="loading"><span></span><span></span><span></span><p>正在读取真实餐单<br><small>LOADING LIVE MENU</small></p></section>';
    try {
      const response = await fetch('/api/family-menu/bootstrap', {credentials: 'same-origin'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
      if (!data.readonly || !Array.isArray(data.days)) throw new Error('Invalid bootstrap response');
      render(data);
    } catch (error) {
      renderError(error);
    }
  }

  document.querySelectorAll('#kSwitch .k-btn').forEach(button => {
    button.classList.toggle('active', button.dataset.location === initialLocation);
  });
  load();
})();
