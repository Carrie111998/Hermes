const CONFIG = Object.freeze({
  SHEET_NAME: 'Orders',
  REQUIRED_COLUMNS: ['Order ID', 'Status', 'Payload', 'Received At'],
  WEBHOOK_URL_PROPERTY: 'WEBHOOK_URL',
});

function getWebhookUrl_() {
  const url = PropertiesService.getScriptProperties().getProperty(CONFIG.WEBHOOK_URL_PROPERTY);
  if (!url) {
    throw new Error(`Missing script property: ${CONFIG.WEBHOOK_URL_PROPERTY}`);
  }
  return url;
}
