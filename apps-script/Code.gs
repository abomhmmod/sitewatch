/**
 * SiteWatch: أزرار «أضف إلى قائمة» تحت فيديوهات يوتيوب في بوت المواقع.
 * يعمل كلّ دقيقة على حسابك في Google: يقرأ الضغطات من تيلغرام، ويضيف الفيديو إلى قائمته باسمك،
 * ثمّ يجعل الزرّ «✅» (أو «⚠️ … أعد الضغط» إن تعذّرت الإضافة).
 *
 * الإعداد مرّةً واحدة:
 *  1. Services ← YouTube Data API v3 (أو ملفّ appsscript.json المرفق).
 *  2. Project Settings ← Script Properties: TELEGRAM_TOKEN = رمز البوت.
 *  3. شغّل setup من المحرّر واسمح: يتأكّد من الحساب، وينشئ المؤقّت كلّ دقيقة.
 */

function setup() {
  const me = YouTube.Channels.list('snippet', { mine: true }).items || [];
  console.log('القناة: ' + (me.length ? me[0].snippet.title + ' ' + (me[0].snippet.customUrl || '') : 'لا قناة في هذا الحساب'));
  const bot = tg_('getMe', {});
  if (!bot.ok) throw new Error('رمز البوت في Script Properties مرفوض: ' + bot.description);
  console.log('البوت: @' + bot.result.username);
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'poll'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(1).create();
  console.log('أُنشئ المؤقّت: poll كلّ دقيقة.');
  poll();
}

function poll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;   // تشغيلٌ سابق لم ينتهِ
  try {
    const props = PropertiesService.getScriptProperties();
    const offset = Number(props.getProperty('OFFSET') || 0);
    const j = tg_('getUpdates', { offset: offset, timeout: 0, allowed_updates: ['callback_query'] });
    if (!j.ok) { console.log('getUpdates: ' + j.description); return; }
    const done = {};
    j.result.forEach(function (u) {
      handle_(u.callback_query, done);
      props.setProperty('OFFSET', String(u.update_id + 1));
    });
  } finally {
    lock.releaseLock();
  }
}

function handle_(cq, done) {
  if (!cq || !cq.message) return;
  const chat = cq.message.chat;
  // الأزرار لا تكون إلّا في رسائل البوت إليك، ومع ذلك: محادثةٌ خاصّة، والضاغط صاحبها
  if (chat.type !== 'private' || cq.from.id !== chat.id) return;
  const m = /^p:([\w-]+):([\w-]{11})$/.exec(cq.data || '');
  if (!m || done[cq.data]) { tg_('answerCallbackQuery', { callback_query_id: cq.id }); return; }
  done[cq.data] = true;

  const markup = cq.message.reply_markup || { inline_keyboard: [] };
  let label = 'القائمة';
  markup.inline_keyboard.forEach(function (row) {
    row.forEach(function (b) { if (b.callback_data === cq.data) label = b.text; });
  });
  label = label.replace(/^[➕⚠️\s]+/u, '').split(' — ')[0];

  let btn, note;
  try {
    YouTube.PlaylistItems.insert({ snippet: { playlistId: m[1], resourceId: { kind: 'youtube#video', videoId: m[2] } } }, 'snippet');
    btn = { text: '✅ ' + label, callback_data: 'x' };
    note = 'أُضيف إلى ' + label;
  } catch (e) {
    console.log('تعذّرت الإضافة إلى «' + label + '»: ' + e.message);
    btn = { text: '⚠️ ' + label + ' — أعد الضغط', callback_data: cq.data };
    note = 'تعذّرت الإضافة: ' + e.message;
  }
  markup.inline_keyboard.forEach(function (row) {
    row.forEach(function (b, i) { if (b.callback_data === cq.data) row[i] = btn; });
  });
  tg_('editMessageReplyMarkup', { chat_id: chat.id, message_id: cq.message.message_id, reply_markup: markup });
  tg_('answerCallbackQuery', { callback_query_id: cq.id, text: note.slice(0, 190) });   // قد يكون قديمًا فيُرفض، ولا بأس
}

function tg_(method, payload) {
  const token = PropertiesService.getScriptProperties().getProperty('TELEGRAM_TOKEN');
  if (!token) throw new Error('ضع TELEGRAM_TOKEN في Project Settings ← Script Properties');
  const r = UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/' + method, {
    method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), muteHttpExceptions: true,
  });
  try { return JSON.parse(r.getContentText()); } catch (e) { return { ok: false, description: 'HTTP ' + r.getResponseCode() }; }
}
