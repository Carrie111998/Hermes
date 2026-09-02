function notifyWebhook_(order) {
  const response = UrlFetchApp.fetch(getWebhookUrl_(), {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      event: 'order.received',
      orderId: order.id,
      status: order.status || 'RECEIVED',
    }),
    muteHttpExceptions: true,
  });
  const statusCode = response.getResponseCode();
  if (statusCode < 200 || statusCode >= 300) {
    throw new Error(`Webhook request failed with HTTP ${statusCode}`);
  }
}
