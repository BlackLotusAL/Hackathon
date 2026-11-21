'use strict';

const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {createContext, runInContext} = require('node:vm');

const html = readFileSync(join(__dirname, '../frontend/index.html'), 'utf8');
const first = html.indexOf('  function removePlayer(');
const last = html.indexOf('  function addLog(', first);
assert(first >= 0 && last > first);

function element(tag, attrs = {}, children = []) {
  return {
    tag, attrs, children, textContent: attrs.text || '',
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    replaceWith() {},
    setAttribute(name, value) { this.attrs[name] = value; }
  };
}

function setup() {
  const nodes = {};
  const players = [];
  const streams = [];
  const context = createContext({
    cur: null, SIDE: ['蓝方', '红方'], el: element,
    $(selector) { return nodes[selector] ||= element(selector); },
    $$() { return []; },
    renderList() {}, refreshList() {}, flushLog() {}, addLog() {}, markRow() {},
    document: {removeEventListener() {}},
    api() { assert.fail('Live initialization must use the stream status'); },
    setTimeout() { assert.fail('A timer must not mount the live player'); },
    Player: function (host) {
      this.extra = element('div');
      this.turn = 0;
      host.appendChild(this.extra);
      this.load = replay => { this.R = replay; };
      this.push = frame => { this.R.frames.push(frame); };
      this.finish = result => { this.R.result = result; };
      this.destroy = () => { this.destroyed = true; };
      this.render = () => {};
      players.push(this);
    },
    EventSource: function (url) {
      this.url = url;
      this.close = () => { this.closed = true; };
      this.send = event => this.onmessage({data: JSON.stringify(event)});
      streams.push(this);
    }
  });
  runInContext(html.slice(first, last), context);
  return {context, nodes, players, streams};
}

const header = {ev: 'header', map: {id: 'R42'}, stage: 'prelim', players: ['A', 'B'], init: {}, humans: []};
const frame = {ev: 'frame', frame: {turn: 1}, entry: {turn: 1}};
function snapshot(events, paused = false) {
  return {ev: 'snapshot', events, status: {paused}};
}

test('the live pane waits for the header even when a snapshot and status arrive first', () => {
  const {context, nodes, players, streams} = setup();
  context.openLive('game');
  const [stream] = streams;
  stream.send(snapshot([], true));
  stream.send({ev: 'status', status: {paused: true}});
  assert.equal(players.length, 0);
  assert.equal(nodes['#stage'].children[0].attrs.role, 'status');
  stream.send(header);
  assert.equal(players.length, 1);
  assert.equal(context.cur.liveState.textContent, '已暂停');
  assert.equal(nodes['#stage'].children[0], context.cur.host);
});

test('a fresh snapshot rebuilds once and a closed session cannot update its replacement', () => {
  const {context, nodes, players, streams} = setup();
  context.openLive('game');
  const [stream] = streams;
  stream.send(snapshot([header, frame]));
  stream.onerror();
  assert.equal(context.cur.liveState.textContent, '正在重新连接');
  stream.send(snapshot([header, frame]));
  assert.equal(players.length, 2);
  assert.equal(players[0].destroyed, true);
  assert.equal(players[1].R.frames.length, 1);
  assert.equal(context.cur.liveState.textContent, '运行中');
  context.openLive('other');
  assert.equal(stream.closed, true);
  stream.send(frame);
  assert.equal(players[1].R.frames.length, 1);
  assert.equal(nodes['#stage'].children[0].textContent, '正在等待对局初始化');
});

test('a startup failure is visible even when the stream ended before a header', () => {
  const {context, nodes, players, streams} = setup();
  context.openLive('game');
  const [stream] = streams;
  stream.send(snapshot([{ev: 'end', result: null, error: 'Bot 未启动'}]));
  assert.equal(players.length, 0);
  assert.equal(stream.closed, true);
  assert.equal(nodes['#stage'].children[0].attrs.role, 'alert');
  assert.equal(nodes['#stage'].children[0].textContent, 'Bot 未启动');
});

test('opening a finished match mounts the final replay directly from its snapshot', () => {
  const {context, nodes, players, streams} = setup();
  context.openLive('finished');
  const [stream] = streams;
  stream.send(snapshot([header, frame, {ev: 'end', result: {winner: 0}}], true));
  assert.equal(players.length, 1);
  assert.equal(players[0].R.frames.length, 1);
  assert.equal(players[0].R.result.winner, 0);
  assert.equal(context.cur.liveState.textContent, '已结束');
  assert.equal(nodes['#stage'].children[0], context.cur.host);
  assert.equal(stream.closed, true);
});
