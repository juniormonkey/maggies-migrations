const SHEET_NAME = 'Pins';
const SECRET_PROP = 'WEBHOOK_SECRET';
const LAST_MSG_PROP = 'LAST_DISCORD_MESSAGE_ID';

const HEADERS = [
  'id',
  'status',
  'place_name',
  'location_query',
  'lat',
  'lng',
  'caption',
  'author',
  'posted_at',
  'layer',
  'media_urls',
  'discord_url',
  'notes',
];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Maggie Map')
    .addItem('Set Webhook Secret', 'promptSetWebhookSecret')
    .addItem('Send Test Alert Email', 'sendTestAlertEmail')
    .addToUi();
}

function promptSetWebhookSecret() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();
  const current = props.getProperty(SECRET_PROP);
  const promptMsg = current
    ? 'A webhook secret is currently set. Enter a new GSHEET_WEBHOOK_SECRET to replace it:'
    : 'Paste the GSHEET_WEBHOOK_SECRET value provided during setup:';
  const result = ui.prompt('Set Webhook Secret', promptMsg, ui.ButtonSet.OK_CANCEL);
  if (result.getSelectedButton() === ui.Button.OK) {
    const val = result.getResponseText().trim();
    if (!val) {
      ui.alert('Secret cannot be empty.');
      return;
    }
    props.setProperty(SECRET_PROP, val);
    ui.alert('Webhook secret saved.');
  }
}

function sendTestAlertEmail() {
  const recipient = Session.getEffectiveUser().getEmail();
  const sheetUrl = SpreadsheetApp.getActiveSpreadsheet().getUrl();
  MailApp.sendEmail({
    to: recipient,
    subject: "[Maggie's Migrations] Test alert email",
    htmlBody:
      '<p>Maggie Map email notifications are working.</p>' +
      `<p><a href="${sheetUrl}">Open Maggie's Migrations Google Sheet</a></p>`,
  });
  SpreadsheetApp.getUi().alert(`Test email sent to ${recipient}`);
}

function doGet(e) {
  try {
    const pins = getApprovedPins_();
    return jsonResponse_({ status: 'ok', pins });
  } catch (err) {
    return jsonResponse_({ status: 'error', error: String(err) });
  }
}

function doPost(e) {
  try {
    const payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const configuredSecret = PropertiesService.getScriptProperties().getProperty(SECRET_PROP);
    if (!configuredSecret || payload.secret !== configuredSecret) {
      return jsonResponse_({ status: 'error', error: 'unauthorized' });
    }

    const action = payload.action || '';
    if (action === 'get_state') {
      return jsonResponse_(handleGetState_());
    }
    if (action === 'sync_batch') {
      return jsonResponse_(handleSyncBatch_(payload));
    }
    return jsonResponse_({ status: 'error', error: `unknown action: ${action}` });
  } catch (err) {
    return jsonResponse_({ status: 'error', error: String(err) });
  }
}

function getPinsSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.getSheets()[0];
    sheet.setName(SHEET_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
  }
  return sheet;
}

function splitMediaUrls_(raw) {
  return String(raw || '')
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function getSheetRows_() {
  const sheet = getPinsSheet_();
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return { sheet, rows: [] };
  }
  const values = sheet.getRange(2, 1, lastRow - 1, HEADERS.length).getValues();
  const rows = values.map((vals, idx) => ({
    rowNumber: idx + 2,
    id: String(vals[0] || '').trim(),
    status: String(vals[1] || '').trim().toLowerCase(),
    place_name: String(vals[2] || '').trim(),
    location_query: String(vals[3] || '').trim(),
    lat: vals[4],
    lng: vals[5],
    caption: String(vals[6] || ''),
    author: String(vals[7] || '').trim(),
    posted_at: vals[8] instanceof Date ? Utilities.formatDate(vals[8], 'UTC', 'yyyy-MM-dd') : String(vals[8] || '').trim(),
    layer: String(vals[9] || '').trim(),
    media_urls: String(vals[10] || '').trim(),
    discord_url: String(vals[11] || '').trim(),
    notes: String(vals[12] || '').trim(),
  }));
  return { sheet, rows };
}

function getApprovedPins_() {
  const { rows } = getSheetRows_();
  return rows
    .filter((r) => {
      const latNum = Number(r.lat);
      const lngNum = Number(r.lng);
      return (
        r.status === 'approved' &&
        r.lat !== '' &&
        r.lng !== '' &&
        !Number.isNaN(latNum) &&
        !Number.isNaN(lngNum)
      );
    })
    .map((r) => ({
      id: r.id,
      place_name: r.place_name,
      lat: Number(r.lat),
      lng: Number(r.lng),
      caption: r.caption,
      author: r.author,
      posted_at: r.posted_at,
      layer: r.layer,
      media: splitMediaUrls_(r.media_urls),
    }));
}

function handleGetState_() {
  const props = PropertiesService.getScriptProperties();
  const lastMessageId = props.getProperty(LAST_MSG_PROP) || '';
  const { rows } = getSheetRows_();

  const existingIds = rows.map((r) => r.id).filter(Boolean);
  const retryGeocoding = rows
    .filter(
      (r) =>
        (r.status === 'needs_geocoding' || r.status === 'pending') &&
        r.location_query &&
        (r.lat === '' || r.lng === '' || Number.isNaN(Number(r.lat)) || Number.isNaN(Number(r.lng)))
    )
    .map((r) => ({
      id: r.id,
      location_query: r.location_query,
      place_name: r.place_name,
    }));

  return {
    status: 'ok',
    last_message_id: lastMessageId,
    existing_ids: existingIds,
    retry_geocoding: retryGeocoding,
    approved_pins: getApprovedPins_(),
  };
}

function handleSyncBatch_(payload) {
  const { sheet, rows } = getSheetRows_();
  const rowById = {};
  rows.forEach((r) => {
    if (r.id) rowById[r.id] = r;
  });

  const geocodedUpdates = Array.isArray(payload.geocoded_updates) ? payload.geocoded_updates : [];
  const appliedUpdates = [];
  geocodedUpdates.forEach((upd) => {
    const existing = rowById[String(upd.id || '')];
    if (!existing) return;
    if (upd.place_name) {
      sheet.getRange(existing.rowNumber, 3).setValue(upd.place_name);
    }
    sheet.getRange(existing.rowNumber, 2).setValue(upd.status || 'pending');
    sheet.getRange(existing.rowNumber, 5).setValue(upd.lat);
    sheet.getRange(existing.rowNumber, 6).setValue(upd.lng);
    sheet
      .getRange(existing.rowNumber, 13)
      .setValue(upd.notes || 'Geocoded from updated location_query — change status to approved');
    appliedUpdates.push({
      id: existing.id,
      place_name: upd.place_name || existing.place_name || existing.location_query,
      status: upd.status || 'pending',
      author: existing.author,
      caption: existing.caption,
      media_urls: existing.media_urls,
      discord_url: existing.discord_url,
    });
  });

  const newRows = Array.isArray(payload.new_rows) ? payload.new_rows : [];
  const appendedRows = [];
  newRows.forEach((row) => {
    const id = String(row.id || '').trim();
    if (!id || rowById[id]) return;
    const values = HEADERS.map((h) => (row[h] !== undefined && row[h] !== null ? row[h] : ''));
    sheet.appendRow(values);
    rowById[id] = true;
    appendedRows.push(row);
  });

  if (payload.last_message_id) {
    PropertiesService.getScriptProperties().setProperty(
      LAST_MSG_PROP,
      String(payload.last_message_id)
    );
  }

  if (appendedRows.length > 0 || appliedUpdates.length > 0) {
    sendAttentionEmail_(appendedRows, appliedUpdates);
  }

  return {
    status: 'ok',
    appended_count: appendedRows.length,
    updated_count: appliedUpdates.length,
    approved_pins: getApprovedPins_(),
  };
}

function escapeHtml_(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function sendAttentionEmail_(appendedRows, appliedUpdates) {
  const recipient = Session.getEffectiveUser().getEmail();
  const sheetUrl = SpreadsheetApp.getActiveSpreadsheet().getUrl();
  const combined = appendedRows.concat(appliedUpdates);

  const needsGeocodingCount = combined.filter((r) => r.status === 'needs_geocoding').length;
  const pendingCount = combined.filter((r) => r.status === 'pending').length;

  const subjectParts = [];
  if (needsGeocodingCount > 0) subjectParts.push(`${needsGeocodingCount} need geocoding`);
  if (pendingCount > 0) subjectParts.push(`${pendingCount} awaiting approval`);
  if (subjectParts.length === 0) subjectParts.push(`${combined.length} updated`);

  const itemBlocks = combined.map((item) => {
    const media = splitMediaUrls_(item.media_urls);
    const thumbHtml = media.length > 0 && !media[0].includes('youtube.com')
      ? `<div style="margin:6px 0;"><img src="${escapeHtml_(media[0])}" width="220" style="border-radius:6px;display:block;"></div>`
      : '';
    const discordHtml = item.discord_url
      ? ` · <a href="${escapeHtml_(item.discord_url)}">Discord message</a>`
      : '';
    const statusBadge = item.status === 'needs_geocoding' ? 'NEEDS GEOCODING' : 'PENDING APPROVAL';
    return (
      `<li style="margin-bottom:16px;">` +
      `<strong>[${statusBadge}] ${escapeHtml_(item.place_name || item.location_query || '(unknown location)')}</strong><br>` +
      `<small>Author: ${escapeHtml_(item.author || 'Unknown')} · Date: ${escapeHtml_(item.posted_at || '')}${discordHtml}</small><br>` +
      `<p style="margin:4px 0;">${escapeHtml_(item.caption || '(no caption)')}</p>` +
      `${thumbHtml}` +
      `</li>`
    );
  });

  const htmlBody =
    `<h2>Maggie's Migrations — Review Needed</h2>` +
    `<p><a href="${sheetUrl}" style="display:inline-block;padding:10px 16px;background:#0288d1;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold;">Open Google Sheet to Review &amp; Approve</a></p>` +
    `<p>Change <code>status</code> from <code>pending</code> to <code>approved</code> to publish to the map. For <code>needs_geocoding</code> rows, fill in <code>location_query</code> (or <code>lat</code>/<code>lng</code>) and set <code>status</code> to <code>approved</code> (or <code>pending</code> to auto-geocode on next run).</p>` +
    `<ul>${itemBlocks.join('')}</ul>`;

  MailApp.sendEmail({
    to: recipient,
    subject: `[Maggie's Migrations] ${subjectParts.join(', ')}`,
    htmlBody,
  });
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}
