# eBay snipe scanner

Searches eBay every 10 minutes for underpriced listings from `watchlist.yaml` and sends buy alerts to Telegram.

## Setup (about 30 minutes, one time)

### 1. Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Save the **bot token** it gives you.
2. Open a chat with your new bot and send it any message.
3. Message **@userinfobot** to get your **chat ID** (a number).

### 2. eBay developer keys
1. Sign up at https://developer.ebay.com with your eBay account.
2. Under **Application Keys**, create a **Production** keyset. eBay asks about "Marketplace Account Deletion" notifications first: choose the exemption option, since this app stores no eBay user data.
3. Save the **App ID (Client ID)** and **Cert ID (Client Secret)**.

### 3. GitHub
1. Create a new **public** repo and upload every file in this folder, keeping the `.github/workflows/scan.yml` path.
   Public repos get free unlimited Actions minutes. A private repo's free 2,000 minutes a month is not enough for a 10-minute schedule.
2. In the repo: **Settings > Secrets and variables > Actions > New repository secret**. Add four secrets:
   `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   Secrets stay hidden even in a public repo. Never put keys in the files themselves.
3. Open the **Actions** tab, enable workflows, pick **scan**, and click **Run workflow** to test.

## Tuning

Everything is in `watchlist.yaml`:
- `expected_sale` is what the item really sells for. The scanner works out the highest price worth paying from it.
- `min_profit` and `min_roi_pct` set how picky it is. Too many alerts: raise them. None: lower them or add watches.
- `exclude_words` drops junk listings.

## Run it locally

```
pip install -r requirements.txt
set the four environment variables, then:
DRY_RUN=1 python scanner.py     # prints alerts without sending them
python -m pytest -q             # runs the tests
```

## Limits

- GitHub's schedule can run several minutes late. For faster checks, run `scanner.py` on your own machine with a 2-minute timer.
- eBay allows about 5,000 searches a day by default. The current watchlist uses about 2,700.
- An alert means "worth a look", not "buy". Always check photos, condition and authenticity yourself.
