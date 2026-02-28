const queryEl = document.getElementById('query');
const resultsEl = document.getElementById('results');
const detailEl = document.getElementById('detail');

const TYPE_COLORS = {
  노말: '#A8A77A', 불꽃: '#EE8130', 물: '#6390F0', 전기: '#F7D02C', 풀: '#7AC74C', 얼음: '#96D9D6',
  격투: '#C22E28', 독: '#A33EA1', 땅: '#E2BF65', 비행: '#A98FF3', 에스퍼: '#F95587', 벌레: '#A6B91A',
  바위: '#B6A136', 고스트: '#735797', 드래곤: '#6F35FC', 악: '#705746', 강철: '#B7B7CE', 페어리: '#D685AD'
};

const statColor = {
  hp: '#ff5959', attack: '#f59e0b', defense: '#3b82f6', 'special-attack': '#8b5cf6', 'special-defense': '#14b8a6', speed: '#10b981'
};

const matchupBadge = (entry) => {
  const color = TYPE_COLORS[entry.type] || '#666';
  return `<span class="type-badge" style="--type-color:${color}">${entry.type} x${entry.multiplier}</span>`;
};

async function search(q) {
  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  return await res.json();
}

async function loadPokemon(id) {
  const res = await fetch(`/api/pokemon/${id}`);
  return await res.json();
}

function tableRowsEggMoves(eggMoves) {
  if (!eggMoves.length) {
    return '<tr><td colspan="7">알기술이 없습니다.</td></tr>';
  }
  return eggMoves.map((m) => {
    const typeColor = TYPE_COLORS[m.type] || '#666';
    return `
      <tr>
        <td>${m.name}</td>
        <td><span class="type-badge" style="--type-color:${typeColor}">${m.type}</span></td>
        <td>${m.damage_class || '-'}</td>
        <td>${m.power || '-'}</td>
        <td>${m.accuracy || '-'}</td>
        <td>${m.pp || '-'}</td>
        <td class="left">${m.effect || '-'}</td>
      </tr>
    `;
  }).join('');
}

function renderDetail(data) {
  const abilityRows = data.abilities.map((a) => `
    <tr>
      <td>${a.name}${a.hidden ? ' <span class="hidden-tag">숨특</span>' : ''}</td>
      <td class="left">${a.description || '설명 정보가 없습니다.'}</td>
    </tr>
  `).join('');

  const statRows = data.stats.map((s) => {
    const color = statColor[s.key] || '#666';
    const width = Math.min((s.value / 255) * 100, 100);
    return `
      <tr>
        <th>${s.name}</th>
        <td class="stat-value">${s.value}</td>
        <td>
          <div class="bar-track"><div class="bar-fill" style="width:${width}%; background:${color}"></div></div>
        </td>
      </tr>
    `;
  }).join('');

  detailEl.innerHTML = `
    <section class="card compact-grid">
      <div class="panel">
        <div class="identity">
          <img src="${data.image}" alt="${data.korean_name}" />
          <div>
            <h2>${data.korean_name} <small>#${data.id}</small></h2>
            <p class="mono">${data.identifier}</p>
            <div>${data.types.map((t) => `<span class="type-badge" style="--type-color:${TYPE_COLORS[t] || '#666'}">${t}</span>`).join(' ')}</div>
          </div>
        </div>

        <h3>특성 / 숨특</h3>
        <p class="desc">특성은 배틀 중 지속적으로 발동하는 고유 능력이고, 숨특은 일반적으로 얻기 어려운 희귀 특성입니다.</p>
        <table class="info-table">
          <thead><tr><th>특성</th><th>설명</th></tr></thead>
          <tbody>${abilityRows}</tbody>
        </table>

        <h3>타입 상성 (약점 / 반감 / 무효)</h3>
        <div class="matchup-block">
          <strong>약점</strong>
          <div>${data.type_matchups.weakness.map(matchupBadge).join(' ') || '없음'}</div>
          <strong>반감</strong>
          <div>${data.type_matchups.resistance.map(matchupBadge).join(' ') || '없음'}</div>
          <strong>무효</strong>
          <div>${data.type_matchups.immune.map(matchupBadge).join(' ') || '없음'}</div>
        </div>
      </div>

      <div class="panel">
        <h3>종족값 분포</h3>
        <table class="info-table">
          <thead><tr><th>능력치</th><th>수치</th><th>분포</th></tr></thead>
          <tbody>${statRows}</tbody>
          <tfoot><tr><th>총합</th><td colspan="2">${data.stat_total}</td></tr></tfoot>
        </table>
      </div>
    </section>

    <section class="card">
      <h3>알기술</h3>
      <p class="desc">알기술은 교배를 통해서만 배울 수 있는 기술입니다. 타입, 분류(물리/특수/변화), 위력, 명중, PP, 효과를 표로 확인하세요.</p>
      <table class="info-table move-table">
        <thead>
          <tr>
            <th>기술명</th><th>타입</th><th>분류</th><th>위력</th><th>명중</th><th>PP</th><th>효과</th>
          </tr>
        </thead>
        <tbody>${tableRowsEggMoves(data.egg_moves)}</tbody>
      </table>
    </section>
  `;
}

let timer;
queryEl.addEventListener('input', () => {
  clearTimeout(timer);
  const q = queryEl.value.trim();
  if (!q) {
    resultsEl.innerHTML = '';
    detailEl.innerHTML = '';
    return;
  }
  timer = setTimeout(async () => {
    const rows = await search(q);
    resultsEl.innerHTML = rows.map((r) => `<li data-id="${r.id}">${r.korean_name}<small>#${r.id}</small></li>`).join('');
  }, 120);
});

resultsEl.addEventListener('click', async (e) => {
  const li = e.target.closest('li');
  if (!li) return;
  const data = await loadPokemon(li.dataset.id);
  renderDetail(data);
});
