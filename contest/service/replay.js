// The match player from the supplied portal, adapted to Contest's v1 archives.
window.ContestReplay = (() => {
  'use strict';
  const view = document.getElementById('match-replay');
  const field = name => document.getElementById('replay-' + name);
  const canvas = field('canvas');
  const slider = field('seek');
  const sideNames = ['蓝方', '红方'];
  const sideCards = [...view.querySelectorAll('.replay-side')];
  const speeds = [...view.querySelectorAll('[data-speed]')];
  const direction = {U: '上', D: '下', L: '左', R: '右'};
  const offsets = {U: [0, -1], D: [0, 1], L: [-1, 0], R: [1, 0]};
  const reasons = {time: '耗时决胜', bonus: '订单奖励决胜', lead_turns: '领先回合数决胜',
    crash: '程序退出', errors: '累计错误', startup: '未就绪'};
  const eventText = {
    close: event => '闭合轮廓，获得 ' + event.gain + ' 分',
    cut: () => '切断对手轮廓',
    selfcut: () => '踩到自身轮廓',
    crash: () => '撞上缺陷或出界',
    collide: () => '束斑相撞',
    void: event => '使对手 ' + event.n + ' 格潜像作废',
    enclose: () => '围住对手轮廓',
    down: () => '停机校准',
    respawn: () => '在校准区重启',
    develop: event => '显影 ' + event.n + ' 格',
    decay: event => event.n + ' 格失效',
    orderspawn: event => '公示订单 ' + event.no,
    orderexpire: event => '订单 ' + event.no + ' 结算 · 蓝 +' + event.got[0] + ' / 红 +' + event.got[1],
    pickup: () => '拾取加速道具',
    boostannounce: event => '预告第 ' + event.at + ' 回合出现加速道具',
    boostspawn: () => '加速道具出现',
    boostgone: () => '加速道具消失',
    end: () => '对局结束'
  };
  let replay, owner, contour, latent, terrain, colors;
  let turn = 0;
  let speed = 12;
  let animation = 0;
  let last = 0;

  function validate(data) {
    if (data?.version !== 'v1') throw new Error('回放版本不受支持');
    const {map, init, frames, players, result} = data;
    const size = map?.size;
    const pair = items => Array.isArray(items) && items.length === 2;
    const grid = (value, pattern) => typeof value === 'string' && value.length === size ** 2 && pattern.test(value);
    const state = record => pair(record?.beams) && record.beams.every(beam =>
      typeof beam?.alive === 'boolean' && Object.hasOwn(direction, beam.dir)) &&
      pair(record.score) && record.score.every(Number.isFinite) && pair(record.bonus);
    if (!Number.isInteger(size) || size < 1 || size > 128 || !Array.isArray(map.terrain) ||
        map.terrain.length !== size || !map.terrain.every(row => typeof row === 'string' && row.length === size && /^[.#*]+$/.test(row)) ||
        !pair(map.calib) || !map.calib.every(pair) || !pair(players) || !state(init) ||
        !grid(init.owner, /^[.01]+$/) || !grid(init.contour, /^[.01]+$/) ||
        (data.stage === 'final' && !grid(init.latent, /^[.1-9]+$/)) ||
        !Array.isArray(frames) || result?.turns !== frames.length || ![0, 1, null].includes(result.winner)) {
      throw new Error('回放数据不完整');
    }
    if (frames.some((frame, index) => !state(frame) || frame.turn !== index + 1 ||
        !Array.isArray(frame.events) || !Array.isArray(frame.d) || frame.d.some(delta =>
          !Array.isArray(delta) || !Number.isInteger(delta[0]) || delta[0] < 0 || delta[0] >= size ** 2 ||
          !['.', '0', '1'].includes(delta[1]) || !['.', '0', '1'].includes(delta[2])))) {
      throw new Error('回放帧无效');
    }
  }

  function resetGrid() {
    owner = [...replay.init.owner];
    contour = [...replay.init.contour];
    latent = [...(replay.init.latent ?? '.'.repeat(owner.length))];
    turn = 0;
  }

  function seek(target) {
    if (target < turn) resetGrid();
    while (turn < target) {
      for (const [index, nextOwner, nextContour, nextLatent = '.'] of replay.frames[turn].d) {
        owner[index] = nextOwner;
        contour[index] = nextContour;
        latent[index] = nextLatent;
      }
      turn += 1;
    }
    render();
  }

  function blend(color, background, alpha) {
    const channels = hex => {
      const digits = hex.slice(1);
      const value = parseInt(digits.length === 3 ? digits.replace(/./g, '$&$&') : digits, 16);
      return [value >> 16 & 255, value >> 8 & 255, value & 255];
    };
    const base = channels(background);
    return 'rgb(' + channels(color).map((value, index) => Math.round(value * alpha + base[index] * (1 - alpha))).join(',') + ')';
  }

  function palette() {
    const css = getComputedStyle(view);
    const read = name => css.getPropertyValue('--' + name).trim();
    const surface = read('surface');
    const sides = [read('blue'), read('red')];
    return {surface, sides, exposed: sides.map(color => blend(color, surface, .72)),
      latent: sides.map(color => blend(color, surface, .28)),
      cell: read('replay-cell'), defect: read('replay-defect'), key: read('replay-key'),
      order: read('replay-order'), boost: read('replay-boost')};
  }

  function drawBoard(state) {
    const width = Math.round(canvas.clientWidth * (window.devicePixelRatio || 1));
    if (!width) return;
    if (canvas.width !== width || canvas.height !== width) canvas.width = canvas.height = width;
    const context = canvas.getContext('2d');
    const size = replay.map.size;
    const cell = width / size;
    const gap = cell * .07;
    const fill = (color, x, y, length) => {
      context.fillStyle = color;
      context.fillRect(x, y, length, length);
    };
    fill(colors.surface, 0, 0, width);
    for (let index = 0; index < terrain.length; index += 1) {
      const x = (index % size) * cell + gap / 2;
      const y = Math.floor(index / size) * cell + gap / 2;
      const length = cell - gap;
      const tile = terrain[index];
      const seat = owner[index];
      if (tile === '#') {
        fill(colors.defect, x, y, length);
        continue;
      }
      const hidden = latent[index] !== '.';
      fill(seat === '.' ? colors.cell : (hidden ? colors.latent : colors.exposed)[seat], x, y, length);
      if (tile === '*') fill(colors.key, x + length * .3, y + length * .3, length * .4);
      if (hidden && seat !== '.' && tile !== '*' && cell >= 12) {
        context.fillStyle = colors.sides[seat];
        context.font = '600 ' + Math.round(cell * .44) + 'px monospace';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(latent[index], x + length / 2, y + length / 2);
      }
      const trail = contour[index];
      if (trail !== '.') fill(colors.sides[trail], x + length * .23, y + length * .23, length * .54);
    }
    context.lineWidth = Math.max(1.5, cell * .12);
    replay.map.calib.forEach(([x, y], seat) => {
      context.strokeStyle = colors.sides[seat];
      context.strokeRect((x - 1) * cell + 1, (y - 1) * cell + 1, 3 * cell - 2, 3 * cell - 2);
    });
    if (state.order) {
      const [left, top, right, bottom] = state.order.rect;
      context.save();
      context.strokeStyle = colors.order;
      context.setLineDash([cell * .5, cell * .3]);
      context.strokeRect(left * cell + 1, top * cell + 1,
        (right - left + 1) * cell - 2, (bottom - top + 1) * cell - 2);
      context.restore();
    }
    const boost = state.boost || state.boost_next;
    if (boost) {
      context.save();
      context.translate((boost.x + .5) * cell, (boost.y + .5) * cell);
      context.rotate(Math.PI / 4);
      context.strokeStyle = colors.boost;
      const half = cell * .45;
      if (state.boost) fill(colors.boost, -half, -half, half * 2);
      else {
        context.setLineDash([cell * .25, cell * .18]);
        context.strokeRect(-half, -half, half * 2, half * 2);
      }
      context.restore();
    }
    state.beams.forEach((beam, seat) => {
      if (!beam.alive) return;
      const x = beam.x * cell;
      const y = beam.y * cell;
      const inset = cell * .08;
      const [dx, dy] = offsets[beam.dir];
      fill(colors.sides[seat], x + inset, y + inset, cell - 2 * inset);
      context.strokeStyle = beam.boost_left > 0 ? colors.boost : colors.surface;
      context.strokeRect(x + inset, y + inset, cell - 2 * inset, cell - 2 * inset);
      context.fillStyle = colors.surface;
      context.beginPath();
      context.arc(x + cell * (.5 + .22 * dx), y + cell * (.5 + .22 * dy), cell * .12, 0, 2 * Math.PI);
      context.fill();
    });
  }

  function renderLegend() {
    const items = [['蓝方图形区', colors.exposed[0]], ['红方图形区', colors.exposed[1]],
      ['基板缺陷', colors.defect], ['关键图形区', colors.key], ['订单区域', colors.order], ['加速道具', colors.boost]];
    if (replay.stage === 'final') items.splice(2, 0, ['蓝方潜像', colors.latent[0]], ['红方潜像', colors.latent[1]]);
    field('legend').replaceChildren(...items.map(([label, color]) => {
      const marker = document.createElement('i');
      marker.style.backgroundColor = color;
      const item = document.createElement('span');
      item.append(marker, label);
      return item;
    }));
  }

  function renderEvents() {
    const rows = [];
    for (let frame = turn; frame > 0 && rows.length < 12; frame -= 1) {
      const events = replay.frames[frame - 1].events;
      for (let index = events.length - 1; index >= 0 && rows.length < 12; index -= 1) {
        const event = events[index];
        if (!Object.hasOwn(eventText, event.t) || (event.t === 'develop' && frame !== turn)) continue;
        const row = document.createElement('li');
        const number = document.createElement('code');
        number.textContent = '#' + frame;
        const side = event.t === 'end' ? '' : sideNames[event.s];
        row.append(number, (side ? side + ' ' : '') + eventText[event.t](event));
        rows.push(row);
      }
    }
    if (!rows.length) {
      const empty = document.createElement('li');
      empty.textContent = '暂无事件';
      rows.push(empty);
    }
    field('events').replaceChildren(...rows);
  }

  function syncPlayback() {
    if (!replay) return;
    const total = replay.frames.length;
    field('play').textContent = animation ? '暂停' : total > 0 && turn === total ? '重播' : '播放';
    field('play').disabled = field('restart').disabled = slider.disabled = total === 0;
    field('back').disabled = turn === 0;
    field('next').disabled = turn === total;
    slider.value = turn;
  }

  function render() {
    const state = replay.frames[turn - 1] ?? replay.init;
    drawBoard(state);
    sideCards.forEach((card, seat) => {
      const beam = state.beams[seat];
      const status = beam.alive ? '（' + beam.x + ', ' + beam.y + '）向' + direction[beam.dir] :
        '停机校准 · ' + beam.restart_in + ' 回合';
      card.querySelector('[data-score]').textContent = state.score[seat];
      card.querySelector('[data-state]').textContent = status +
        (beam.boost_left ? ' · 加速 ' + beam.boost_left : '') + (state.bonus[seat] ? ' · 订单 +' + state.bonus[seat] : '');
    });
    field('turn').textContent = '第 ' + turn + ' / ' + replay.frames.length + ' 回合';
    const {winner, reason} = replay.result;
    const result = winner === null ? '未决胜' : sideNames[winner] + '获胜';
    field('result').textContent = turn === replay.frames.length ? result + (reasons[reason] ? ' · ' + reasons[reason] : '') : '';
    field('order').toggleAttribute('data-active', Boolean(state.order));
    field('order').textContent = state.order ? '订单 ' + state.order.no + ' · ' + (state.order.until - turn) + ' 回合后到期' : '暂无订单';
    const boost = state.boost || state.boost_next;
    field('boost').toggleAttribute('data-active', Boolean(boost));
    field('boost').textContent = boost ? '加速道具 · ' + (state.boost ? '已出现' : (boost.at - turn) + ' 回合后出现') +
      '（' + boost.x + ', ' + boost.y + '）' : '暂无加速道具';
    renderEvents();
    syncPlayback();
  }

  function pause() {
    cancelAnimationFrame(animation);
    animation = 0;
    syncPlayback();
  }

  function tick(now) {
    if (now - last >= 1000 / speed) {
      last = now;
      seek(turn + 1);
    }
    if (turn < replay.frames.length) animation = requestAnimationFrame(tick);
    else pause();
  }

  function play() {
    if (!replay?.frames.length || animation) return;
    if (turn === replay.frames.length) seek(0);
    last = performance.now();
    animation = requestAnimationFrame(tick);
    syncPlayback();
  }

  field('play').addEventListener('click', () => animation ? pause() : play());
  field('restart').addEventListener('click', () => { pause(); seek(0); play(); });
  field('back').addEventListener('click', () => { pause(); seek(turn - 1); });
  field('next').addEventListener('click', () => { pause(); seek(turn + 1); });
  slider.addEventListener('input', () => {
    const target = Number(slider.value);
    pause();
    seek(target);
  });
  for (const button of speeds) button.addEventListener('click', () => {
    speed = Number(button.dataset.speed);
    for (const choice of speeds) choice.setAttribute('aria-pressed', String(choice === button));
  });
  window.addEventListener('resize', () => { if (replay) drawBoard(replay.frames[turn - 1] ?? replay.init); });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!replay) return;
    colors = palette();
    renderLegend();
    render();
  });

  return {
    load(data, label) {
      validate(data);
      pause();
      replay = data;
      terrain = replay.map.terrain.join('');
      resetGrid();
      field('match').textContent = label + ' · ' + replay.map.id + ' · ' + (replay.stage === 'final' ? '决赛' : '初赛');
      sideCards.forEach((card, seat) => { card.querySelector('[data-team]').textContent = replay.players[seat]; });
      slider.max = replay.frames.length;
      view.hidden = false;
      colors = palette();
      renderLegend();
      render();
      play();
    },
    clear() {
      pause();
      replay = undefined;
      view.hidden = true;
    }
  };
})();
