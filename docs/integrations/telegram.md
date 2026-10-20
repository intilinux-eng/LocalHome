# Telegram (the board's bot)

Every LocalHome can have a Telegram bot of its own. From a phone,
anywhere, it gives:

- **the home at a glance**, as the top of the home page shows it: what
  the plant is doing, outside, each zone (temperature, humidity, what
  it's doing, its target and until when), the dehumidifier, the VMC, the
  meters, what needs looking at;
- **the simple commands**: a zone's temperature by hand, a zone off or
  back on its schedule, away for a few days and back, the VMC's speed (or
  a speed for a while), the dehumidifier;
- **a message when something needs looking at**: the problems the home
  page lists (a sensor that stopped answering, a battery running down,
  the board's disk...) once they've lasted 5 minutes, and "solved" when
  they're gone; the power alert too, if `energy:` is set up.

It's set up from the admin panel, section **Telegram** - no file to edit.

## One bot per LocalHome

Each board needs a bot of its own. Telegram hands a bot's messages to one
reader at a time: two boards on the same token would take turns getting
them (each told "409 Conflict") and the commands would land on either. A
bot is free, and an account can have up to 20.

To follow several homes from one chat, put each home's bot in a Telegram
group: the messages arrive there, and each bot answers the commands
addressed to it (`/status@rossi_home_bot`).

## 1. Create the bot with @BotFather

1. In Telegram, open [@BotFather](https://t.me/BotFather) (the blue tick
   is Telegram's own) and send it `/newbot`.
2. It asks for a **name**: the one you'll see in chats, e.g. `Rossi Home`.
3. Then a **username**: unique on Telegram, ending in `bot`, e.g.
   `rossi_home_bot`.
4. It answers with the **token**, a line like
   `123456789:AAH...` - the bot's key. Copy it; don't send it to anyone.

Optional, still in @BotFather: `/setuserpic` for a picture,
`/setdescription` for what someone opening the bot reads first. Leave
`/setprivacy` as it is (enabled): in a group the bot then reads only the
commands addressed to it.

## 2. Connect it in the admin panel

1. Open the admin panel (`/climate-admin`), section **Telegram**.
2. Paste the token, press **Connect the bot**. The board tries it with
   Telegram first: a token mistyped, or one Telegram doesn't know, is
   refused and nothing is saved. Once connected the section says
   "Connected as @rossi_home_bot".
3. In Telegram, open the bot (search its username) and press **Start**.
   The bot doesn't answer yet - no one is authorized - but the chat shows
   up in the admin panel under **Wrote to the bot** within a few seconds.
4. Press **Authorize**. From then on the bot answers that chat. Each
   authorized chat has two choices:
   - **Notifications**: it gets the problems and the alerts;
   - **Commands**: it may change things. Without it, it only looks -
     the overview and the problems, nothing else (e.g. for a relative who
     only needs to know).
5. **Test message** sends one to that chat now: the way to check a chat
   gets the notifications.

Every member of the family does steps 3-4 from their own Telegram. A
group works the same way (add the bot to the group, send `/start` in it,
authorize the group) - but then anyone in the group uses the bot.

The token and the chats are kept in `config/secrets_telegram.json`, on
the board only. The admin panel never shows the token again: **Change
token** takes a new one, **Remove the bot** deletes both from the board
(the bot itself stays on Telegram - `/deletebot` in @BotFather removes
it).

## 3. Using it

Send `/status` (`/stato` on an Italian board), or press **Start**: the
overview comes with buttons under it - refresh, away / I'm back, each
zone, the VMC, the dehumidifier. Most of the time a tap is enough:

- a zone's buttons: **−** and **+** half a degree, **Switch off**, and
  **Schedule** when it was changed by hand. A change by hand lasts until
  the schedule's next change - or 4 hours if the week doesn't change - as
  the home page's first choice does;
- away: 1 day, 3 days, 1 week; then **I'm back**;
- the VMC: each speed, a top speed for 30 minutes to 2 hours, on/off;
- the dehumidifier: its durations, "until the target", off (in its
  season only).

Or typed - the board's language and English both work:

| Command | Does |
|---|---|
| `/status` | the overview |
| `/zone Rooms 21` | 21° by hand, until the schedule's next change |
| `/zone Rooms 21 3h` | 21° for 3 hours (up to 24) |
| `/zone Rooms off` | off, until the schedule's next change |
| `/zone Rooms schedule` | back to its week |
| `/away 3` | away for 3 days (`/away 12h`: 12 hours) |
| `/back` | I'm back |
| `/vmc day` | the VMC at its day speed (`min`, `night`, `day`, `high`, `boost`, or a percentage) |
| `/vmc boost 30` | top speed for 30 minutes, then back |
| `/vmc off`, `/vmc on`, `/vmc cancel` | off, on, end a timed speed |
| `/dehumidifier 2` | on for 2 hours (one of its durations) |
| `/dehumidifier target`, `/dehumidifier off` | until the target humidity, off |
| `/problems` | what needs looking at |
| `/help` | the commands |

A zone can be written in part (`/zone bat` for "Bathrooms") and in any
case. Every command goes through the same checks as the home page's
buttons, and a refusal is said in the board's language ("You're away:
the zones stay off...").

A command written while the board couldn't hear it - switched off, or
without internet - isn't carried out when it arrives more than 5 minutes
later ("away" from yesterday evening may not be wanted any more): the
bot says so, and it can be sent again.

## Is it safe?

- **No port opened.** The board asks Telegram for the bot's messages
  (HTTPS, out, like a browser); nothing on the internet can reach the
  board. Exposing the home page on the internet instead would be far
  riskier.
- **Only the chats authorized** in the admin panel get an answer - the
  chat's id comes from Telegram's servers and can't be faked. Anyone else
  who finds the bot gets not a word.
- **The bot does only what the home page does**, with the same checks
  (5-35 °C, at most a day by hand...). Nothing of the admin panel:
  devices, settings, the season, restarts.
- **Chats with bots are not end-to-end encrypted**: they're encrypted on
  the way, but Telegram can read them on its servers - the temperatures,
  and when the home is empty (away).
- **The real risk is your Telegram account**: whoever gets into it
  commands the bot. Turn on two-step verification in Telegram (Settings
  → Privacy and Security → Two-Step Verification).
- **The token is the bot's key.** It stays on the board; the admin panel
  never shows it, the log never prints it. If it gets out: `/revoke` in
  @BotFather, then **Change token** with the new one.
- **Handing a board over** to someone else: **Remove the bot** first - a
  factory reset keeps credentials, the bot's included.

For a channel with no third party at all, a VPN to the home network
(WireGuard, Tailscale) opens the home page itself from afar - more to set
up.

## When it doesn't work

The section's header says how the bot is:

| It says | Means |
|---|---|
| Connected as @... | working |
| Telegram can't be reached | the board has no internet, or Telegram is down: it keeps trying (every 2 minutes at most) |
| Something else reads the bot's messages | the same token is in use elsewhere - another LocalHome, an old script. Each board needs a bot of its own: create another one, or stop the other program |
| Telegram no longer accepts the token | revoked in @BotFather: paste the new one with **Change token** |

A **Test message** that fails with "Forbidden: bot can't initiate
conversation with a user" means that chat never pressed **Start** in the
bot - or blocked it.

The board's log says when the bot connects, when it can't, and who
changed what from Telegram (`journalctl -u localhome | grep Telegram`).

## Older setups

A `notifications:` section in `config.yaml` from before the bot,

```yaml
notifications:
  type: telegram
  secrets_file: "secrets_telegram.json"
```

now only says where the bot's file is (the default is the same), and a
file with one `"chat_id"` reads as that chat, authorized with
notifications and commands. Nothing else is needed: connect the bot in
the admin panel to see it there.
