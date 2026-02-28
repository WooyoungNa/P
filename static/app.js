const queryEl = document.getElementById('query');
const resultsEl = document.getElementById('results');
const detailEl = document.getElementById('detail');

async function search(q) {
  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  return await res.json();
}

async function loadPokemon(id) {
  const res = await fetch(`/api/pokemon/${id}`);
  return await res.json();
}

function renderDetail(data) {
  const statsHtml = data.stats.map(s => `
    <div class="row"><strong>${s.name}</strong><span>${s.value}</span>
      <div class="bar-wrap"><div class="bar" style="width:${Math.min((s.value / 255) * 100, 100)}%"></div></div>
    </div>`).join('');

  const abilities = data.abilities.map(a => `<li>${a.name}${a.hidden ? ' (숨겨진 특성)' : ''}</li>`).join('');
  const eggMoves = data.egg_moves.length ? data.egg_moves.map(m => `<li>${m}</li>`).join('') : '<li>없음</li>';

  detailEl.innerHTML = `
    <div class="card">
      <div class="top">
        <img src="${data.image}" alt="${data.korean_name}" />
        <div>
          <h2>${data.korean_name} (#${data.id})</h2>
          <p>${data.identifier}</p>
          <div class="badges">${data.types.map(t => `<span>${t}</span>`).join('')}</div>
        </div>
      </div>

      <h3>종족값 분포</h3>
      <div class="stats">${statsHtml}</div>

      <h3>특성 (+숨특)</h3>
      <ul class="tags">${abilities}</ul>

      <h3>특성상 약점 / 반감 / 무효</h3>
      <p><strong>약점:</strong> ${data.type_matchups.weakness.join(', ') || '없음'}</p>
      <p><strong>반감:</strong> ${data.type_matchups.resistance.join(', ') || '없음'}</p>
      <p><strong>무효:</strong> ${data.type_matchups.immune.join(', ') || '없음'}</p>

      <h3>알기술</h3>
      <ul class="tags">${eggMoves}</ul>
    </div>`;
}

let timer;
queryEl.addEventListener('input', () => {
  clearTimeout(timer);
  const q = queryEl.value.trim();
  if (!q) {
    resultsEl.innerHTML = '';
    return;
  }
  timer = setTimeout(async () => {
    const rows = await search(q);
    resultsEl.innerHTML = rows.map(r => `<li data-id="${r.id}">${r.korean_name}</li>`).join('');
  }, 120);
});

resultsEl.addEventListener('click', async (e) => {
  const li = e.target.closest('li');
  if (!li) return;
  const data = await loadPokemon(li.dataset.id);
  renderDetail(data);
});
