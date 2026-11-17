/**
 * SiteWatch: أزرار «أضف إلى قائمة» تحت فيديوهات يوتيوب في بوت المواقع.
 * يعمل كلّ دقيقة على حسابك في Google: يقرأ الضغطات من تيلغرام، ويضيف الفيديو إلى قائمته باسمك،
 * ثمّ يجعل الزرّ «✅» (أو «⚠️ … أعد الضغط» إن تعذّرت الإضافة).
 *  - إن كان الفيديو في القائمة من قبل لا يُضاف ثانيةً («موجود مسبقًا»).
 *  - الضغط على «✅» يحذفه من القائمة ويعيد الزرّ «➕» (للتراجع عن ضغطةٍ خاطئة).
 *  - رسالةٌ واحدة مثبّتة في موضوع يوتيوب تتعدّل بصمت: عدد فيديوهات كلّ قائمة من 5000 وآخر عمليّة.
 *
 * الإعداد مرّةً واحدة:
 *  1. Services ← YouTube Data API v3 (أو ملفّ appsscript.json المرفق).
 *  2. Project Settings ← Script Properties: TELEGRAM_TOKEN = رمز البوت.
 *  3. شغّل setup من المحرّر واسمح: يتأكّد من الحساب، وينشئ المؤقّت كلّ دقيقة.
 */

const MAX_ITEMS = 5000;   // أقصى ما يقبله يوتيوب في القائمة الواحدة

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
    const done = {}, boards = {};
    j.result.forEach(function (u) {
      handle_(u.callback_query, done, boards);
      props.setProperty('OFFSET', String(u.update_id + 1));
    });
    Object.keys(boards).forEach(function (k) {
      try { board_(boards[k]); } catch (e) { console.log('رسالة القوائم: ' + e.message); }
    });
  } finally {
    lock.releaseLock();
  }
}

function handle_(cq, done, boards) {
  if (!cq || !cq.message) return;
  const chat = cq.message.chat;
  // الأزرار في رسائل البوت إليك: في المحادثة الخاصّة يُقبل ضغط صاحبها، وفي مجموعة المواضيع ضغط مُنشئها وحده
  if (chat.type === 'private') {
    if (cq.from.id !== chat.id) return;
  } else {
    const mem = tg_('getChatMember', { chat_id: chat.id, user_id: cq.from.id });
    if (!mem.ok || mem.result.status !== 'creator') {
      tg_('answerCallbackQuery', { callback_query_id: cq.id, text: 'هذه الأزرار لصاحب المجموعة وحده' });
      return;
    }
  }
  // p: أضف، u: احذف (زرّ «✅»). والأزرار القديمة «✅» بلا تراجع (x) تُترك كما هي.
  const m = /^([pu]):([\w-]+):([\w-]{11})$/.exec(cq.data || '');
  if (!m || done[cq.data]) { tg_('answerCallbackQuery', { callback_query_id: cq.id }); return; }
  done[cq.data] = true;
  const add = m[1] === 'p', pid = m[2], vid = m[3];

  const markup = cq.message.reply_markup || { inline_keyboard: [] };
  let label = 'القائمة';
  markup.inline_keyboard.forEach(function (row) {
    row.forEach(function (b) { if (b.callback_data === cq.data) label = b.text; });
  });
  label = label.replace(/^[➕✅⚠️\s]+/u, '').split(' — ')[0];
  const title = title_(vid);

  let btn, note, line;
  try {
    const have = YouTube.PlaylistItems.list('id', { playlistId: pid, videoId: vid, maxResults: 50 }).items || [];
    if (add) {
      if (have.length) {
        note = 'موجود مسبقًا في ' + label;
        line = '☑️ ' + title + ' موجود مسبقًا في «' + esc_(label) + '»';
      } else {
        YouTube.PlaylistItems.insert({ snippet: { playlistId: pid, resourceId: { kind: 'youtube#video', videoId: vid } } }, 'snippet');
        note = 'أُضيف إلى ' + label;
        line = '✅ أُضيف ' + title + ' إلى «' + esc_(label) + '»';
      }
      btn = { text: '✅ ' + label, callback_data: 'u:' + pid + ':' + vid };
    } else {
      have.forEach(function (it) { YouTube.PlaylistItems.remove(it.id); });
      note = have.length ? 'حُذف من ' + label : 'ليس في ' + label;
      line = '↩️ ' + (have.length ? 'حُذف ' + title + ' من' : title + ' ليس في') + ' «' + esc_(label) + '»';
      btn = { text: '➕ ' + label, callback_data: 'p:' + pid + ':' + vid };
    }
  } catch (e) {
    console.log('تعذّر ' + (add ? 'إضافة' : 'حذف') + ' الفيديو في «' + label + '»: ' + e.message);
    btn = { text: '⚠️ ' + label + ' — أعد الضغط', callback_data: cq.data };
    note = 'تعذّر ' + (add ? 'الإضافة' : 'الحذف') + ': ' + e.message;
    line = '⚠️ تعذّر ' + (add ? 'إضافة ' + title + ' إلى' : 'حذف ' + title + ' من') + ' «' + esc_(label) + '»';
  }
  markup.inline_keyboard.forEach(function (row) {
    row.forEach(function (b, i) { if (b.callback_data === cq.data) row[i] = btn; });
  });
  tg_('editMessageReplyMarkup', { chat_id: chat.id, message_id: cq.message.message_id, reply_markup: markup });
  tg_('answerCallbackQuery', { callback_query_id: cq.id, text: note.slice(0, 190) });   // قد يكون قديمًا فيُرفض، ولا بأس

  const thread = cq.message.message_thread_id || 0;
  const key = chat.id + '_' + thread;
  (boards[key] = boards[key] || { chat: chat.id, thread: thread, lines: [] }).lines.push(line);
}

/** رسالة القوائم المثبّتة في الموضوع الذي ضُغط فيه: تُعدَّل بصمت، وتُرسل وتُثبَّت أوّل مرّة أو إن حُذفت. */
function board_(b) {
  const props = PropertiesService.getScriptProperties();
  const key = 'BOARD_' + b.chat + '_' + b.thread;
  const pls = YouTube.Playlists.list('snippet,contentDetails', { mine: true, maxResults: 50 }).items || [];
  const text = '📋 <b>قوائمي في يوتيوب</b>\n' +
    pls.map(function (p) { return '• ' + esc_(p.snippet.title) + ': ' + p.contentDetails.itemCount + ' / ' + MAX_ITEMS; }).join('\n') +
    '\n\n<b>آخر عمليّة</b> (' + Utilities.formatDate(new Date(), 'Asia/Damascus', 'HH:mm') + '):\n' +
    b.lines.slice(-5).join('\n');
  const opts = { chat_id: b.chat, text: text, parse_mode: 'HTML', link_preview_options: { is_disabled: true } };

  const mid = Number(props.getProperty(key) || 0);
  if (mid) {
    const r = tg_('editMessageText', Object.assign({ message_id: mid }, opts));
    if (r.ok || /not modified/i.test(r.description || '')) return;
    if (!/not found|MESSAGE_ID_INVALID|can't be edited/i.test(r.description || '')) {
      console.log('editMessageText: ' + r.description);   // خطأٌ عابر: لا تُرسل رسالةً ثانية
      return;
    }
  }
  if (b.thread) opts.message_thread_id = b.thread;
  opts.disable_notification = true;
  const s = tg_('sendMessage', opts);
  if (!s.ok) { console.log('sendMessage: ' + s.description); return; }
  props.setProperty(key, String(s.result.message_id));
  const p = tg_('pinChatMessage', { chat_id: b.chat, message_id: s.result.message_id, disable_notification: true });
  if (!p.ok) console.log('pinChatMessage: ' + p.description);
}

/** عنوان الفيديو رابطًا قصيرًا إليه، أو «الفيديو» إن تعذّرت قراءته. */
function title_(vid) {
  let t = 'الفيديو';
  try {
    const v = YouTube.Videos.list('snippet', { id: vid }).items || [];
    if (v.length) t = v[0].snippet.title;
  } catch (e) { /* يكفي الاسم العامّ */ }
  if (t.length > 70) t = t.slice(0, 69) + '…';
  return '<a href="https://youtu.be/' + vid + '">' + esc_(t) + '</a>';
}

function esc_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function tg_(method, payload) {
  const token = PropertiesService.getScriptProperties().getProperty('TELEGRAM_TOKEN');
  if (!token) throw new Error('ضع TELEGRAM_TOKEN في Project Settings ← Script Properties');
  let r;
  try {
    r = UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/' + method, {
      method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), muteHttpExceptions: true,
    });
  } catch (e) {   // رسالة الخطأ فيها الرابط وفيه الرمز، فلا تُسجَّل كما هي
    throw new Error('تعذّر الاتّصال بتيلغرام (' + method + '): ' + String(e.message).split(token).join('***'));
  }
  try { return JSON.parse(r.getContentText()); } catch (e) { return { ok: false, description: 'HTTP ' + r.getResponseCode() }; }
}
