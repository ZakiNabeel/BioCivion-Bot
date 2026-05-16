# 🤖 BioCivion Telegram Store Bot

> A fully automated Telegram store bot built with Python — handling orders, payments, discounts, and admin tracking without manual intervention.

![BioCivion Bot Banner](biocivion_screenshot2.png)

---

## ✨ Features

### 🛒 Automated Ordering & Volume Discounts
- Customers place orders directly through Telegram chat
- Automatic volume discount calculation and application
- Real-time order confirmation with subtotal breakdown
- Seamless ordering experience with no manual handling needed

### 💳 Secure, Multi-Gateway Payments (Advanced)
- Supports **Square**, **Stripe**, and **Zelle**
- 20-minute payment timer with automated auto-cancel on timeout
- Secure payment flow integrated directly into the bot

### 📊 Admin Management & Tracking
- Full order management dashboard for admins
- Real-time order status tracking (Log Full, etc.)
- Daily tracking notifications sent automatically at **9PM CST**
- All orders logged and accessible via admin commands

![Feature Overview](biocivion_screenshot1.png)

---

## 🛠️ Tech Stack

| Technology | Usage |
|---|---|
| Python | Core bot logic |
| Telegram Bot API | Bot interface & messaging |
| PythonAnywhere | Cloud deployment & hosting |
| Google Sheets | Inventory & order data management |

---

## 🚀 Getting Started

### Prerequisites
- Python 3.8+
- A Telegram Bot Token (via [@BotFather](https://t.me/BotFather))
- PythonAnywhere account (for deployment)

### Installation

```bash
# Clone the repository
git clone https://github.com/ZakiNabeel/BioCivion-Bot.git
cd BioCivion-Bot

# Install dependencies
pip install -r requirements.txt

# Configure your environment variables
cp .env.example .env
# Edit .env with your bot token and credentials
```

### Configuration

Create a `.env` file with the following:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_CHAT_ID=your_admin_chat_id
PAYMENT_TIMEOUT=1200  # 20 minutes in seconds
```

### Running the Bot

```bash
python bot.py
```

---

## 📦 Deployment (PythonAnywhere)

1. Upload project files to PythonAnywhere
2. Set up a **Always-on task** pointing to `bot.py`
3. Configure environment variables in the dashboard
4. The bot will run 24/7 with automatic restarts

---

## 📋 Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and main menu |
| `/order` | Start a new order |
| `/status` | Check order status |
| `/cancel` | Cancel current order |
| `/admin` | Admin panel (restricted) |

---

## ⭐ Client Testimonial

> *"Zaki is a consummate Professional. Was very timely, communication was very open. Executed the project assignment with consistency. I will definitely rehire Zaki. I was very lucky to work with him!! Job very well done!!!"*
>
> — **Lisa Bond, BioCivion** | ⭐⭐⭐⭐⭐ on Upwork

---

## 👨‍💻 Author

**Zaki Nabeel**
- GitHub: [@ZakiNabeel](https://github.com/ZakiNabeel)
- Upwork: [View Profile](https://www.upwork.com/freelancers/zakinabeel)
- Email: zakinabeelalu@gmail.com

---

## 📄 License

This project is available for reference. Please contact the author for commercial use.
