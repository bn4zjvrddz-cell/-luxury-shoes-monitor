# Luxury Shoes Monitor — GitHub Actions

Безкоштовний монітор OLX + Shafa → Telegram.

## Поточні фільтри

- 55 брендів luxury / designer
- EU 36–41
- 300–4000 грн
- усе, крім оголошень, які явно позначені як нові
- слова original/authentic/оригінал не обов'язкові
- туфлі, підбори, босоніжки, лофери, балетки, чоботи,
  напівчоботи, ботильйони, мюлі тощо
- спортивне взуття відсіюється

## Як працює

GitHub Actions запускає `monitor.py` кожні 5 хвилин.
За один запуск перевіряється 20 брендів, тому весь список проходиться
приблизно за 15 хвилин (GitHub іноді може затримувати scheduled jobs).

`state.json` зберігає лише ID уже побачених оголошень та технічний стан.
Telegram token у файлах НЕ зберігається.

## Secrets

У GitHub Repository:
Settings → Secrets and variables → Actions → New repository secret

Створити:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Перший запуск

Actions → Luxury Shoes Monitor → Run workflow.

Після успішного підключення бот один раз надішле:
`✅ Luxury Shoes Monitor підключено...`

Поточні оголошення під час першого проходу записуються як baseline,
щоб Telegram не засипало старими товарами.
