import { prepareAlbumItems, sendAlbumSequence } from './bridge_helpers.js';

export function registerAlbumRoute(app, {
  getSocket,
  getConnectionState,
  enqueueSend,
  trackSentMessageId,
  messageStore,
}) {
  app.post('/send-album', async (req, res) => {
    const socket = getSocket();
    if (!socket || getConnectionState() !== 'connected') {
      return res.status(503).json({
        success: false,
        attempted: false,
        status: 'not_connected',
        error: 'Not connected to WhatsApp',
      });
    }

    const { chatId, items } = req.body;
    if (!chatId || !Array.isArray(items)) {
      return res.status(400).json({
        success: false,
        attempted: false,
        status: 'validation_error',
        error: 'chatId and items are required',
      });
    }

    let preparedItems;
    try {
      preparedItems = prepareAlbumItems(items);
    } catch (error) {
      return res.status(400).json({
        success: false,
        attempted: false,
        status: 'validation_error',
        error: error.message,
      });
    }

    const result = await enqueueSend(() => sendAlbumSequence({
      chatId,
      items: preparedItems,
      // Do not race album socket writes against a timer. A losing sendMessage
      // promise cannot be cancelled; releasing the global queue would allow it
      // to overlap later sends and recreate cross-chat contamination.
      send: async (targetChatId, payload) => {
        const sent = await socket.sendMessage(targetChatId, payload);
        trackSentMessageId(sent);
        messageStore.remember(sent);
        return sent;
      },
    }));

    const statusCode = result.success ? 200 : (result.status === 'partial_failure' ? 207 : 502);
    return res.status(statusCode).json(result);
  });
}
