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
  const reasons = {time: '耗时决胜', lead_turns: '领先回合数决胜',
    crash: '程序退出', errors: '累计错误', startup: '未就绪'};
  const eventText = {
    close: event => '闭合轮廓，新领地面值 ' + event.gain,
    cut: () => '切断对手轮廓',
    selfcut: () => '踩到自身轮廓',
    crash: () => '撞上缺陷或出界',
    collide: () => '束斑相撞',
    down: () => '停机校准',
    respawn: () => '在校准区重启',
    end: () => '对局结束'
  };
  let replay, owner, contour, terrain, colors;
  let turn = 0;
  let speed = 12;
  let animation = 0;
  let last = 0;

  function validate(data) {
    if (data?.version !== 'v1' || data.stage !== 'prelim' || data.params?.station_period !== 100) {
      throw new Error('回放须使用当前赛题规则');
    }
    const {map, init, frames, players, result} = data;
    const size = map?.size;
    const pair = items => Array.isArray(items) && items.length === 2;
    const coordinate = items => pair(items) && items.every(value => Number.isInteger(value) && value >= 0 && value < size);
    const counters = items => pair(items) && items.every(Number.isInteger);
    const grid = (value, pattern) => typeof value === 'string' && value.length === size ** 2 && pattern.test(value);
    const state = record => pair(record?.beams) && record.beams.every(beam =>
      typeof beam?.alive === 'boolean' && Object.hasOwn(direction, beam.dir)) &&
      counters(record.score) && counters(record.lead_turns) && counters(record.time_us) && coordinate(record.active_station);
    if (!Number.isInteger(size) || size < 1 || size > 128 || !Array.isArray(map.terrain) ||
        map.terrain.length !== size || !map.terrain.every(row => typeof row === 'string' && row.length === size && /^[.#*]+$/.test(row)) ||
        !pair(map.calib) || !map.calib.every(pair) || !pair(players) || !state(init) ||
        !grid(init.owner, /^[.01]+$/) || !grid(init.contour, /^[.01]+$/) ||
        !Array.isArray(frames) || result?.turns !== frames.length || ![0, 1, null].includes(result.winner)) {
      throw new Error('回放数据不完整');
    }
    if (!counters(result.lead_turns) || !counters(result.time_us) || !Number.isInteger(data.params.max_turns)) {
      throw new Error('回放缺少终局计分指标');
    }
    if (!Array.isArray(map.boost_points) || map.boost_points.length !== 3 || !map.boost_points.every(coordinate)) {
      throw new Error('回放缺少固定加速站');
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
    turn = 0;
  }

  function seek(target) {
    if (target < turn) resetGrid();
    while (turn < target) {
      for (const [index, nextOwner, nextContour] of replay.frames[turn].d) {
        owner[index] = nextOwner;
        contour[index] = nextContour;
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
      cell: read('replay-cell'), defect: read('replay-defect'), key: read('replay-key'),
      station: read('replay-station')};
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
    const station = state.active_station;
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
      fill(seat === '.' ? colors.cell : colors.exposed[seat], x, y, length);
      if (station && Math.abs(index % size - station[0]) <= 1 && Math.abs(Math.floor(index / size) - station[1]) <= 1) {
        context.strokeStyle = colors.station;
        context.lineWidth = Math.max(1, cell * .14);
        context.strokeRect(x + cell * .1, y + cell * .1, length - cell * .2, length - cell * .2);
      }
      if (tile === '*') fill(colors.key, x + length * .3, y + length * .3, length * .4);
      const trail = contour[index];
      if (trail !== '.') fill(colors.sides[trail], x + length * .23, y + length * .23, length * .54);
    }
    context.lineWidth = Math.max(1.5, cell * .12);
    replay.map.calib.forEach(([x, y], seat) => {
      context.strokeStyle = colors.sides[seat];
      context.strokeRect((x - 1) * cell + 1, (y - 1) * cell + 1, 3 * cell - 2, 3 * cell - 2);
    });
    const [blue, red] = state.beams;
    const sharedCell = blue.alive && red.alive && blue.x === red.x && blue.y === red.y;
    state.beams.forEach((beam, seat) => {
      if (!beam.alive) return;
      const inset = cell * .08;
      const height = cell - 2 * inset;
      const width = sharedCell ? height / 2 : height;
      const x = beam.x * cell + inset + (sharedCell ? seat * width : 0);
      const y = beam.y * cell + inset;
      const [dx, dy] = offsets[beam.dir];
      context.fillStyle = colors.sides[seat];
      context.fillRect(x, y, width, height);
      context.strokeStyle = colors.surface;
      context.strokeRect(x, y, width, height);
      context.fillStyle = colors.surface;
      context.beginPath();
      context.arc(x + width * (.5 + .22 * dx), y + height * (.5 + .22 * dy),
        sharedCell ? cell * .08 : cell * .12, 0, 2 * Math.PI);
      context.fill();
    });
  }

  function renderLegend() {
    const items = [['蓝方图形区', colors.exposed[0]], ['红方图形区', colors.exposed[1]],
      ['基板缺陷', colors.defect], ['关键图形区', colors.key], ['点亮的加速区', colors.station]];
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
        if (!Object.hasOwn(eventText, event.t)) continue;
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
      const totals = turn === replay.frames.length ? replay.result : state;
      const details = ' · 领先 ' + totals.lead_turns[seat] + ' 回合 · 累计 ' + totals.time_us[seat] + ' 微秒';
      card.querySelector('[data-state]').textContent = status + details;
    });
    field('turn').textContent = '第 ' + turn + ' / ' + replay.frames.length + ' 回合';
    const {winner, reason} = replay.result;
    const result = winner !== null ? sideNames[winner] + '获胜' : reason === 'double_loss' ? '双方判负' : '待裁定';
    field('result').textContent = turn === replay.frames.length ? result + (reasons[reason] ? ' · ' + reasons[reason] : '') : '';
    const [x, y] = state.active_station;
    const round = Math.min(turn + 1, replay.params.max_turns);
    const station = Math.floor((round - 1) / replay.params.station_period) % 3;
    field('station').textContent = '第 ' + round + ' 回合亮区 · ' + 'ABC'[station] + ' 站（' + x + ', ' + y + '）';
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
      field('match').textContent = label + ' · ' + replay.map.id + ' · ' + '初赛';
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
