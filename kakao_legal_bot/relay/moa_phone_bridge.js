/**
 * 모아 폰 브리지 — 루팅 없는 공기계(예: 갤럭시 S25)를 에뮬레이터 대신 쓴다.
 *
 * 메신저봇R(MessengerBot R) 안에서 도는 스크립트입니다. 하는 일은 두 가지:
 *
 *   1. 카카오톡 알림으로 들어온 메시지를 서버(/iris/webhook)로 전달하고
 *   2. 서버가 큐에 쌓아둔 답변(/outbox)을 주기적으로 가져와
 *      알림 답장(quick-reply) 기능으로 해당 방에 전송합니다.
 *
 * 루팅이 필요 없는 이유: 안드로이드의 공식 알림 읽기/알림 답장 API만 씁니다.
 * 대신 눈이 '알림'이므로 — 이 폰의 카카오톡 알림은 반드시 켜져 있어야 하고
 * (내용 미리보기 포함), 이 폰에서 봇 계정의 채팅방을 열어 읽으면 안 됩니다
 * (열려 있는 방은 알림이 안 떠서 그 메시지를 놓칩니다). PHONE.md 참조.
 *
 * 설치: 메신저봇R → 새 봇 만들기(API2, 메신저봇 자바스크립트) → 이 파일
 * 내용 붙여넣기 → 아래 CONFIG 세 값을 Railway 와 같게 → 컴파일 → 전원 ON.
 */

// ─── CONFIG — 이 세 줄만 고치면 됩니다 ────────────────────────────────────
var SERVER = "https://<앱이름>.up.railway.app"; // PUBLIC_BASE_URL 과 동일
var WEBHOOK_SECRET = "여기에 IRIS_WEBHOOK_SECRET 값";
var OUTBOX_TOKEN = "여기에 OUTBOX_TOKEN 값";
var POLL_MS = 2000; // 답변 큐 확인 주기(ms). 1500~3000 권장.
// ─────────────────────────────────────────────────────────────────────────

var MAP_PATH = "/sdcard/msgbot/moa_rooms.json"; // 방 id → 방 이름 매핑 저장

var bot = BotManager.getCurrentBot();
var roomNames = loadMap();

// ─── HTTP (메신저봇R 에 내장된 Jsoup 사용) ───────────────────────────────
function httpPost(url, headers, bodyObj) {
  var conn = org.jsoup.Jsoup.connect(url)
    .ignoreContentType(true)
    .ignoreHttpErrors(true)
    .timeout(15000)
    .header("Content-Type", "application/json")
    .requestBody(JSON.stringify(bodyObj))
    .method(org.jsoup.Connection.Method.POST);
  for (var key in headers) conn.header(key, headers[key]);
  var res = conn.execute();
  return { code: res.statusCode(), body: res.body() };
}

function httpGet(url, headers) {
  var conn = org.jsoup.Jsoup.connect(url)
    .ignoreContentType(true)
    .ignoreHttpErrors(true)
    .timeout(15000)
    .method(org.jsoup.Connection.Method.GET);
  for (var key in headers) conn.header(key, headers[key]);
  var res = conn.execute();
  return { code: res.statusCode(), body: res.body() };
}

// ─── 방 id ↔ 이름 매핑 (재부팅을 견디게 파일로) ──────────────────────────
function loadMap() {
  try {
    var raw = FileStream.read(MAP_PATH);
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    return {};
  }
}

function rememberRoom(id, name) {
  if (!id || !name || roomNames[id] === name) return;
  roomNames[id] = name;
  try {
    FileStream.write(MAP_PATH, JSON.stringify(roomNames));
  } catch (e) {
    Log.e("moa: 방 매핑 저장 실패 " + e);
  }
}

// ─── 1. 수신 → 서버 웹훅 ─────────────────────────────────────────────────
function onMessage(msg) {
  try {
    var channelId = "";
    try {
      channelId = msg.channelId ? String(msg.channelId) : "";
    } catch (e) {}
    var roomId = channelId || msg.room;
    rememberRoom(roomId, msg.room);

    var payload = {
      room_id: roomId,
      room: msg.room,
      sender: msg.author ? msg.author.name : "",
      msg: msg.content,
      type: "1",
      // 알림 기반이라 카톡의 log_id 를 모릅니다. 항상 유일한 값을 만들어
      // 보내야 서버의 중복제거가 "같은 말 두 번"을 오판하지 않습니다.
      log_id: "phone-" + Date.now() + "-" + Math.floor(Math.random() * 100000),
      chat_type: msg.isGroupChat ? "group" : "direct",
      timestamp: Math.floor(Date.now() / 1000)
    };

    var res = httpPost(
      SERVER + "/iris/webhook",
      { "X-Iris-Secret": WEBHOOK_SECRET },
      payload
    );
    if (res.code >= 400) Log.e("moa: webhook " + res.code + " " + res.body);
  } catch (e) {
    Log.e("moa: webhook 전송 실패 " + e);
  }
}

bot.addListener(Event.MESSAGE, onMessage);

// ─── 2. 서버 답변 큐 → 알림 답장으로 전송 ────────────────────────────────
function pollOutbox() {
  var data;
  try {
    var res = httpGet(SERVER + "/outbox?limit=10", { "X-Outbox-Token": OUTBOX_TOKEN });
    if (res.code >= 400) return;
    data = JSON.parse(res.body);
  } catch (e) {
    return; // 서버 재배포·전파 끊김 — 다음 주기에 다시
  }
  var messages = (data && data.messages) || [];
  if (!messages.length) return;

  var delivered = [];
  var failed = [];
  for (var i = 0; i < messages.length; i++) {
    var item = messages[i];
    // 답장은 '방 이름'으로만 보낼 수 있습니다. 우리 매핑 → 서버가 준
    // room_name → 마지막으로 room 값 그대로, 순서로 시도합니다.
    var name = roomNames[item.room] || item.room_name || item.room;
    var ok = false;
    try {
      ok = Api.replyRoom(name, item.text, true);
    } catch (e) {
      ok = false;
    }
    (ok ? delivered : failed).push(item.id);
    java.lang.Thread.sleep(400); // 같은 순간 도착하면 카톡이 순서를 섞습니다
  }

  try {
    if (delivered.length)
      httpPost(SERVER + "/outbox/ack", { "X-Outbox-Token": OUTBOX_TOKEN },
        { ids: delivered, ok: true });
    if (failed.length)
      httpPost(SERVER + "/outbox/ack", { "X-Outbox-Token": OUTBOX_TOKEN },
        { ids: failed, ok: false, error: "replyRoom failed (알림 세션 없음?)" });
  } catch (e) {
    Log.e("moa: ack 실패 " + e); // ack 못 하면 서버가 알아서 재큐합니다
  }
}

// ─── 폴링 타이머 (재컴파일 때 중복 타이머가 남지 않게) ───────────────────
var pollTimer = new java.util.Timer();
pollTimer.schedule(
  new java.util.TimerTask({ run: function () { try { pollOutbox(); } catch (e) {} } }),
  3000,
  POLL_MS
);

function onStartCompile() {
  try { pollTimer.cancel(); } catch (e) {}
}
