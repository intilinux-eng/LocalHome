# Telegram (notifications)

Covers the `telegram` (`notifier`) driver, used for the power-budget
alert described in
[docs/configuration.md](../configuration.md#energy-power-budget-alerting).
A bot's `sendMessage` API is a single HTTPS POST with no OAuth/session
dance, which makes it a simple default notification channel.

## 1. Create a bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) ->
   `/newbot` -> follow the prompts. You get a **bot token** back, shaped
   like `123456789:AAExampleTokenFromBotFather`.
2. Start a chat with your new bot (search its username, send anything).

## 2. Find your chat_id

The simplest way: send your bot a message, then visit
`https://api.telegram.org/bot<your bot token>/getUpdates` in a browser
and read `result[0].message.chat.id` from the JSON response.

## 3. Configure credentials

```bash
cp config/secrets_telegram.example.json config/secrets_telegram.json
```

```json
{
  "bot_token": "123456789:AAExampleTokenFromBotFather",
  "chat_id": "123456789"
}
```

## 4. Enable the driver

```yaml
notifications:
  type: telegram
  secrets_file: "secrets_telegram.json"
```

If this file is missing or invalid, LocalHome logs a warning at startup
and keeps running with notifications silently disabled - a broken/absent
notifier never blocks the rest of the dashboard.
