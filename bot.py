"""
BioCivion Telegram Store Bot
- Inventory via Google Sheets
- Square + Stripe + Zelle payments
- Quantity-based reservation system (no Django needed)
- 2-min entry timers, 20-min payment timer with auto-cancel
- Payment method locking (Square/Stripe/Zelle locked on selection)
- Full customer info collection + address confirmation
- Multi-item cart with $20 flat shipping
- Per-item volume discounts (5-9 = 10% off, 10+ = 15% off)
- /cancel command at any point, auto-cancel on inactivity

config.py must include:
  TELEGRAM_TOKEN, SPREADSHEET_ID, SHEET_NAME, CREDENTIALS_FILE,
  SQUARE_ACCESS_TOKEN, SQUARE_LOCATION_ID, SQUARE_ENVIRONMENT,
  STRIPE_SECRET_KEY,
  ZELLE_BUSINESS_TAG, ZELLE_BUSINESS_NAME,
  PAYMENT_TIMEOUT_MINUTES
"""

import logging
import random
import string
import datetime
import pytz
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from sheets import (
    get_items,
    reduce_inventory,
    log_new_order,
    log_cancelled_order,
    find_order,
    set_payment_method,
    cancel_order_in_sheet,
    get_expired_pending_orders,
    get_pending_tracking,
    mark_notification_sent,
    get_pending_verification,
    mark_verification_sent,
    clear_payment_expiry,
    get_user_history,
    check_promo_code,
)
from payments import (
    create_square_link,
    cancel_square_link,
    create_stripe_session,
    cancel_stripe_session,
    get_zelle_message,
    SHIPPING,
)
from locks import reserve, release, release_all, get_available, get_user_reserved
from config import TELEGRAM_TOKEN, PAYMENT_TIMEOUT_MINUTES, ADMIN_EMAIL, SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SHIPPING_COST = 20.00
ENTRY_TIMEOUT = 120                            # 2 minutes per data-entry step
PAYMENT_TIMEOUT = PAYMENT_TIMEOUT_MINUTES * 60  # 20 minutes default

US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
    "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
    "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
    "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
]

TERMS_AND_CONDITIONS = (
    "📋 *Terms and Conditions*\n\n"
    "🌡️ *Research Use Only Peptides*\n"
    "Our products are used solely for research purposes. If this is not your intended use of the product, please, do not place an order.\n\n"
    "🔬 *Accepted US Based Only Test Vendors*\n"
    "Only test results from US based Testing Labs are recognized and acknowledged.\n\n"
    "🔎💯 *Purity Guarantee*\n"
    "Always over 99% Purity. This guarantee is for 30 days from the date you receive the product.\n\n"
    "📄 *Testing Results Requirements*\n"
    "🫧 Include Purity and Mass Results\n"
    "📸 Pictures of vials submitted\n"
    "📈 Show the HPLC Chromatogram (purity & quantity)\n"
    "💹 Show the Mass Spectrometry (MS) Spectrum (identity of substance & structure)\n\n"
    "☑️ *Issue Resolution*\n"
    "- If there is a problem with your order, pls send message to @BioCivion.\n"
    "- The choice between reshipment or refund is determined based on the nature and scope of the issue.\n\n"
    "Read the above *Terms and Conditions* carefully before proceeding. Thank You"
)

HEADS_UP = (
    "✨⚠️ *Just a heads\\-up, before we continue:*\n\n"
    "📍 Shipping is restricted to *US/North America addresses* only\n"
    "📦 Flat rate shipping fee is *$20*\n"
    "💳 We accept *Zelle*, *Square*, or *Stripe*\n"
    "🕒 Orders completed Mon\\-Fri before *1PM EST* ship same day\n"
    "🗓️ Orders *after 1PM EST* or on weekends ship the *next business day*\n"
    "🚚 Shipments normally arrive in *3 business days*\n"
    "📧 Tracking numbers are sent *Monday\\-Friday*\n\n"
    "Ready to go? Click *Proceed with Order* ⬇️\\. If not, no worries—just type */cancel* to exit\\.\n"
    "If you have any questions, just message @BioCivion 💬\\."
)

# ── Steps ──────────────────────────────────────────────────────────────────────
STEP_MENU          = "menu"
STEP_QTY           = "qty"
STEP_EMAIL         = "email"
STEP_SMS           = "sms"
STEP_FIRST_NAME    = "first_name"
STEP_LAST_NAME     = "last_name"
STEP_STREET        = "street"
STEP_APT           = "apt"
STEP_CITY          = "city"
STEP_STATE         = "state"
STEP_ZIP           = "zip"
STEP_CONFIRM_ADDR  = "confirm_addr"
STEP_PAYMENT       = "payment"
STEP_PAYMENT_WAIT  = "payment_wait"   # waiting for user to complete Square/Stripe/Zelle
STEP_CANCEL_ID     = "cancel_order_id"
STEP_CANCEL_LOOKUP = "cancel_lookup"


# ── Discount helpers ───────────────────────────────────────────────────────────
# ── Discount helpers ───────────────────────────────────────────────────────────

def _cart_total_qty(cart: list[dict]) -> int:
    return sum(item["qty"] for item in cart)

def _get_cart_discount_rate(cart: list[dict]) -> float:
    total_qty = _cart_total_qty(cart)
    if total_qty >= 10:
        return 0.15
    elif total_qty >= 5:
        return 0.10
    return 0.0


# ── Cart helpers ───────────────────────────────────────────────────────────────
def _cart_summary(cart: list[dict]) -> str:
    lines = []
    raw_subtotal = 0.0
    
    for item in cart:
        orig = item["original_price"]
        qty = item["qty"]
        line_total = orig * qty
        raw_subtotal += line_total
        lines.append(f"• {qty}x {item['name']} - ${orig:.2f} each (${line_total:.2f})")
    
    rate = _get_cart_discount_rate(cart)
    if rate > 0:
        pct = int(rate * 100)
        discount_amount = raw_subtotal * rate
        lines.append(f"\n🏷️ *Volume Discount ({pct}% off): -${discount_amount:.2f}*")
        
    return "\n".join(lines)

def _cart_subtotal(cart: list[dict]) -> float:
    raw_subtotal = sum(i["original_price"] * i["qty"] for i in cart)
    rate = _get_cart_discount_rate(cart)
    return round(raw_subtotal * (1.0 - rate), 2)

def _gen_order_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def _expiry_iso() -> str:
    """Return ISO timestamp string PAYMENT_TIMEOUT_MINUTES from now (UTC)."""
    expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
    return expiry.isoformat()


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _build_menu_keyboard(items: list, user_id: int = None, refreshed: bool = False):
    header   = "🛍️ *Please select a product:*" + (" _(refreshed)_" if refreshed else "")
    keyboard = []
    for item in items:
        name      = item["name"]
        price     = item["price"]
        stock     = item["quantity"]
        invoice   = item.get("invoice_name", "")
        available = get_available(name, stock)
        if available > 0:
            keyboard.append([InlineKeyboardButton(
                f"{name} ${price:.2f} (qty: {available})",
                callback_data=f"buy_{name}"
            )])
    keyboard.append([InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")])
    return header, InlineKeyboardMarkup(keyboard)

def _payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☑️ Pay via Square",  callback_data="pay_square")],
        [InlineKeyboardButton("✅ Pay via Stripe",  callback_data="pay_stripe")],
        [InlineKeyboardButton("💸 Pay via Zelle",   callback_data="pay_zelle")],
        [InlineKeyboardButton("❌ Cancel Order",     callback_data="cancel_btn")],
    ])

def _main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["✅ Payment Complete"]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


# ── Timer helpers ──────────────────────────────────────────────────────────────

async def _cancel_session(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    reason: str = "manual",
):
    """Release all reservations, cancel any active payment links, clear session, notify user."""
    release_all(user_id)

    # Cancel active payment links if any
    confirmed = context.user_data.get("confirmed_order")
    if confirmed:
        order_id     = confirmed.get("order_id", "")
        pay_method   = context.user_data.get("payment_method", "")
        square_id    = context.user_data.get("square_link_id")
        stripe_sid   = context.user_data.get("stripe_session_id")

        if pay_method == "Square" and square_id:
            cancel_square_link(square_id)
        elif pay_method == "Stripe" and stripe_sid:
            cancel_stripe_session(stripe_sid)

        if order_id:
            cancel_order_in_sheet(order_id)

    # Kill all timers
    for job_name in [f"entry_timer_{user_id}", f"payment_timer_{user_id}"]:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    context.user_data.clear()

    if reason == "inactivity":
        msg = (
            "⏰ *Order Cancelled Due to Inactivity*\n\n"
            "Your session timed out and your cart has been cleared.\n\n"
            "🔹 Type /start to restart your order\n"
            "🔹 Type /cancel to exit"
        )
    elif reason == "expired":
        order_id = confirmed.get("order_id", "") if confirmed else ""
        msg = (
            f"⌛ *Order #{order_id} Has Expired*\n\n"
            f"Your payment window of {PAYMENT_TIMEOUT_MINUTES} minutes has passed and your order has been cancelled.\n\n"
            f"You may now type /start to begin a new order and choose a different payment method."
        )
    else:
        msg = (
            "🔴 *Order Cancelled Successfully*\n\n"
            "Your cart has been cleared and no order was placed.\n\n"
            "🔹 Type /start to restart your order\n"
            "🔹 Type /cancel to exit"
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _entry_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await _cancel_session(context, data["chat_id"], data["user_id"], reason="inactivity")

async def _payment_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await _cancel_session(context, data["chat_id"], data["user_id"], reason="expired")

def _reset_entry_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    job_name = f"entry_timer_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(
        _entry_timeout_job, when=ENTRY_TIMEOUT,
        data={"chat_id": chat_id, "user_id": user_id}, name=job_name,
        chat_id=chat_id,  # <-- Injects context.chat_data into the job
        user_id=user_id   # <-- Injects context.user_data into the job
    )

def _start_payment_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Starts the 20-minute payment window timer."""
    for job in context.job_queue.get_jobs_by_name(f"entry_timer_{user_id}"):
        job.schedule_removal()

    job_name = f"payment_timer_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(
        _payment_timeout_job, when=PAYMENT_TIMEOUT,
        data={"chat_id": chat_id, "user_id": user_id}, name=job_name,
        chat_id=chat_id,  
        user_id=user_id   
    )
def _stop_payment_timer(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    for job in context.job_queue.get_jobs_by_name(f"payment_timer_{user_id}"):
        job.schedule_removal()


async def _promo_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    
    # Temporarily change the step so any random text isn't counted as a promo code
    context.user_data["step"] = "promo_timeout"
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Time's up! ⏰\n"
            "The window to enter your promo code has expired. How would you like to proceed? "
            "Select button below or Type '/cancel' to cancel order."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Try entering code one more time 🔄", callback_data="retry_promo")],
            [InlineKeyboardButton("Continue to Checkout 🛒", callback_data="skip_promo")]
        ])
    )

def _start_promo_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    # Stop the standard 2-minute entry timer
    for job in context.job_queue.get_jobs_by_name(f"entry_timer_{user_id}"):
        job.schedule_removal()
        
    job_name = f"promo_timer_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
        
    # START TIMER (Added chat_id and user_id to prevent crashing)
    context.job_queue.run_once(
        _promo_timeout_job, when=60,  # 1 MINUTE LIMIT
        data={"chat_id": chat_id, "user_id": user_id}, name=job_name,
        chat_id=chat_id,   # <-- THIS WAS MISSING
        user_id=user_id    # <-- THIS WAS MISSING
    )

async def _start_address_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the email/history checkout process after promo code logic is finished."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Resume the standard 2-minute timer
    _reset_entry_timer(context, chat_id, user_id)
    
    history = get_user_history(str(chat_id))
    if history and history.get("email"):
        context.user_data["history"] = history
        context.user_data["step"] = "ask_history"
        
        apt_line = f", Unit {history['apt']}" if history.get("apt") else ""
        address = f"{history['street']}{apt_line}, {history['city']}, {history['state']}, {history['zip']}"
        
        await context.bot.send_message(
            chat_id,
            f"📝 <b>Welcome back! We found your saved details:</b>\n\n"
            f"👤 <b>Name:</b> {history['first_name']} {history['last_name']}\n"
            f"📧 <b>Email:</b> {history['email']}\n"
            f"📱 <b>Phone:</b> {history['sms']}\n"
            f"🏠 <b>Address:</b> {address}\n\n"
            f"Would you like to use these saved details for this order?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, use these details", callback_data="use_history")],
                [InlineKeyboardButton("❌ No, let me enter new details", callback_data="enter_new")],
                [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")]
            ]),
        )
    else:
        context.user_data["step"] = STEP_EMAIL
        await context.bot.send_message(
            chat_id,
            "📧 *Step 1 of 8 — Email Address*\n\nPlease enter your email address:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )


def send_order_email(order_id: str, customer: dict, pay_method: str, total: float):
    """Sends an email alert to the admin when an order is completed."""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USERNAME
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = f"🚨 NEW ORDER ALERT: #{order_id}"

        body = (
            f"NEW ORDER ALERT\n\n"
            f"Order ID: {order_id}\n"
            f"Customer: {customer['first_name']} {customer['last_name']}\n"
            f"Email: {customer.get('email', 'N/A')}\n"
            f"Phone: {customer.get('sms', 'N/A')}\n"
            f"Payment Method: {pay_method}\n"
            f"Total Paid: ${total:.2f}\n\n"
            f"Please check the Google Sheet for full order and shipping details."
        )
        msg.attach(MIMEText(body, 'plain'))

        # Connect to server and send
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"Admin email sent successfully for order {order_id}")
    except Exception as e:
        logger.error(f"Failed to send admin order email for {order_id}: {e}")

# ── Command handlers ───────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    release_all(user_id)
    context.user_data.clear()

    await update.message.reply_text(
        "Welcome to the BioCivion Bot! Please review our terms below.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        TERMS_AND_CONDITIONS,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ I Agree & Continue", callback_data="agree_terms")
        ]]),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # If the user has any active state, cancel immediately without asking for Order ID
    if (
        context.user_data.get("confirmed_order")
        or context.user_data.get("cart")
        or context.user_data.get("pending_item")
        or context.user_data.get("step") not in [None, STEP_MENU]
    ):
        await _cancel_session(context, chat_id, user_id)
    else:
        await update.message.reply_text(
            "Press /start to begin.",
            reply_markup=ReplyKeyboardRemove(),
        )

async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_items()
    if not items:
        await update.message.reply_text("No items found.")
        return
    lines = ["📦 *Current Inventory:*\n"]
    for item in items:
        available = get_available(item["name"], item["quantity"])
        reserved  = item["quantity"] - available
        status    = (
            "SOLD OUT"
            if item["quantity"] <= 0
            else f"{item['quantity']} in stock ({reserved} reserved)"
        )
        lines.append(f"• {item['name']} — {status} @ ${item['price']:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Button handler ─────────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # ── Terms ─────────────────────────────────────────────────────────────────
    if data == "agree_terms":
        await query.edit_message_text(
            TERMS_AND_CONDITIONS + "\n\n✅ _Terms accepted._", parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id, HEADS_UP, parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Proceed with Order", callback_data="proceed")
            ]]),
        )
        return

    # ── Proceed to menu ───────────────────────────────────────────────────────
    if data == "proceed":
        context.user_data["step"] = STEP_MENU
        context.user_data["cart"] = []
        items = get_items()
        text, markup = _build_menu_keyboard(items, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    # ── Refresh ───────────────────────────────────────────────────────────────
    if data == "refresh":
        items = get_items()
        text, markup = _build_menu_keyboard(items, user_id, refreshed=True)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    # ── Item selected ─────────────────────────────────────────────────────────
    if data.startswith("buy_"):
        item_name = data[len("buy_"):]
        items     = get_items()
        item      = next((i for i in items if i["name"] == item_name), None)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return

        available = get_available(item_name, item["quantity"])
        if available <= 0:
            await query.answer(
                "⚠️ This item is currently fully reserved by other customers. Please try again shortly.",
                show_alert=True,
            )
            return

        context.user_data["pending_item"] = {
            "name":      item["name"],
            "price":     item["price"],
            "stock":     item["quantity"],
            "available": available,
            "invoice":   item.get("invoice_name", ""),
        }
        context.user_data["step"] = STEP_QTY
        _reset_entry_timer(context, chat_id, user_id)

        await query.edit_message_text(
            f"✅ *{item['name']}*\n\n"
            f"💰 Price: ${item['price']:.2f} each\n"
            f"📦 Stock Quantity: {available}\n\n"
            f"🔢 How many would you like to order?\n"
            f"Please enter a number between 1 and {available}\n\n"
            f"_Or click the button below to continue shopping._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Continue Shopping", callback_data="continue_shopping")],
                [InlineKeyboardButton("❌ Cancel",             callback_data="cancel_btn")],
            ]),
        )
        return

    # ── Continue shopping ─────────────────────────────────────────────────────
# ── Continue shopping ─────────────────────────────────────────────────────
    if data == "continue_shopping":
        context.user_data.pop("pending_item", None)
        context.user_data["step"] = STEP_MENU
        for job in context.job_queue.get_jobs_by_name(f"entry_timer_{user_id}"):
            job.schedule_removal()

        items = get_items()
        cart_keyboard = []
        for i in items:
            avail = get_available(i["name"], i["quantity"])
            if avail > 0:
                cart_keyboard.append([InlineKeyboardButton(
                    f"{i['name']} ${i['price']:.2f} (qty: {avail})",
                    callback_data=f"buy_{i['name']}"
                )])
        
        # 🟢 NEW: Only add the Checkout button if the cart has items
        if context.user_data.get("cart"):
            cart_keyboard.append([InlineKeyboardButton("✅ Proceed to Checkout", callback_data="checkout")])
            
        cart_keyboard.append([InlineKeyboardButton("❌ Cancel Order",         callback_data="cancel_btn")])

        await query.edit_message_text(
            "🛍️ *Please select a product:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(cart_keyboard),
        )
        return

    # ── Checkout ──────────────────────────────────────────────────────────────
    if data == "checkout":
        if context.user_data.get("confirmed_order"):
            await query.answer(
                "⚠️ An order has already been created! Please complete payment or cancel it.",
                show_alert=True,
            )
            return

        cart = context.user_data.get("cart", [])
        if not cart:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id, "🛒 Your cart is empty. Use /start to begin.")
            return

        # Live recheck reservations
        items     = get_items()
        stock_map = {i["name"]: i["quantity"] for i in items}
        problems  = []

        for cart_item in cart:
            name    = cart_item["name"]
            wanted  = cart_item["qty"]
            stock   = stock_map.get(name, 0)
            success, available = reserve(name, user_id, wanted, stock)
            if not success:
                problems.append({"name": name, "wanted": wanted, "available": available})

        if problems:
            problem_names = {p["name"] for p in problems}
            cart          = [c for c in cart if c["name"] not in problem_names]
            context.user_data["cart"] = cart
            problem_lines = "\n".join(
                f"• *{p['name']}* — you wanted {p['wanted']}, only {p['available']} available"
                for p in problems
            )

            if not cart:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ *Oops! Quick update on your order*\n\n"
                    f"🔥 The following items are no longer available in the quantity you selected:\n{problem_lines}\n\n"
                    f"🛒 Your cart is now empty. Use /start to begin a new order.",
                    parse_mode="Markdown",
                )
                release_all(user_id)
                context.user_data.clear()
                return

            subtotal = _cart_subtotal(cart)
            total    = subtotal + SHIPPING_COST
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id,
                f"⚠️ *Oops! Quick update on your order*\n\n"
                f"🔥 The item(s) below are in high demand and are being updated:\n{problem_lines}\n\n"
                f"✅ We removed those item(s). Would you like to continue checkout with the updated order?\n\n"
                f"🛒 *Updated Cart:*\n{_cart_summary(cart)}\n\n"
                f"💰 Subtotal: ${subtotal:.2f}\n🚚 Shipping: ${SHIPPING_COST:.2f}\n💵 *Total: ${total:.2f}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Complete My Checkout", callback_data="checkout")],
                    [InlineKeyboardButton("❌ Cancel order",         callback_data="cancel_btn")],
                ]),
            )
            return

        # All good — check for saved history
        # All good — check for saved history
        context.user_data["step"] = "promo_ask"
        _reset_entry_timer(context, chat_id, user_id)
        
        try:
            await query.message.delete()
        except Exception:
            pass
            
        await context.bot.send_message(
            chat_id,
            "Do you have a promo code to apply? 🎫\n"
            "(Use the two buttons below)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, I have one", callback_data="has_promo")],
                [InlineKeyboardButton("No / Skip", callback_data="skip_promo")]
            ])
        )
        return

# ── Promo Code Handlers ───────────────────────────────────────────────────
    if data in ["has_promo", "retry_promo"]:
        context.user_data["step"] = "promo_enter"
        # Track attempts so we can cut them off after 2 tries
        context.user_data["promo_attempts"] = context.user_data.get("promo_attempts", 0)
        
        _start_promo_timer(context, chat_id, user_id)
        
        try:
            await query.message.delete()
        except Exception:
            pass
            
        await context.bot.send_message(
            chat_id,
            "Please type your promo code below ✍️\n"
            "or type '/cancel' to cancel order.\n\n"
            "⏳️*Note: You have 1 minute to enter the code before this session expires.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Continue to payment 💸", callback_data="skip_promo")]
            ])
        )
        return
        
    if data == "skip_promo":
        # Kill promo timer if it's running
        for job in context.job_queue.get_jobs_by_name(f"promo_timer_{user_id}"):
            job.schedule_removal()
            
        try:
            await query.message.delete()
        except Exception:
            pass
            
        # This triggers the function we built in Step 4
        await _start_address_flow(update, context)
        return

    
    # ── History Selection ─────────────────────────────────────────────────────
    if data == "use_history":
        history = context.user_data.get("history")
        if not history:
            await query.answer("Session expired.", show_alert=True)
            return
            
        # Push the history data instantly into the user's active session
        context.user_data.update(history)
        context.user_data["step"] = STEP_CONFIRM_ADDR
        _reset_entry_timer(context, chat_id, user_id)
        
        try:
            await query.message.delete()
        except Exception:
            pass
        # Skip the 8 entry steps and go straight to final cart summary!
        await _show_address_confirmation(update, context)
        return
        
    if data == "enter_new":
        context.user_data["step"] = STEP_EMAIL
        _reset_entry_timer(context, chat_id, user_id)
        
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id,
            "📧 *Step 1 of 8 — Email Address*\n\nPlease enter your email address:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # ── Post-Payment Cancellation from Order Lookup ───────────────────────────
    if data == "confirm_cancel_no":
        context.user_data.pop("pending_cancel_data", None)
        await query.edit_message_text("✅ Cancellation aborted. Your order is safe!")
        return

    if data == "confirm_cancel_yes":
        order_data = context.user_data.get("pending_cancel_data")
        if not order_data:
            await query.answer("Session expired.", show_alert=True)
            return
        log_cancelled_order(order_data["raw_row"])
        await query.edit_message_text(
            f"🚫 Order *#{order_data['order_id']}* has been officially cancelled.\n\n"
            f"Use /start to begin a new order.",
            parse_mode="Markdown",
        )
        context.user_data.pop("pending_cancel_data", None)
        return

    # ── Address confirm / redo ────────────────────────────────────────────────
    if data == "addr_confirm":
        await _proceed_to_final_review(update, context, via_callback=True)
        return

    if data == "addr_redo":
        context.user_data["step"] = STEP_STREET
        _reset_entry_timer(context, chat_id, user_id)
        await query.edit_message_text(
            "📍 *Re-enter Shipping Address*\n\nPlease enter your street address (no apartment/unit number):",
            parse_mode="Markdown",
        )
        return

    # ── Skip apartment ────────────────────────────────────────────────────────
    if data == "skip_apt":
        context.user_data["apt"]  = ""
        context.user_data["step"] = STEP_CITY
        _reset_entry_timer(context, chat_id, user_id)
        await query.edit_message_text("🏙️ *Step 7 of 8 — City*\n\nPlease enter your city:", parse_mode="Markdown")
        return
    

    # ── Handle 'Yes, checkout without this item' from QTY Step ────────────────
    if data == "qty_checkout_without_item":
        problem_names = context.user_data.get("problem_names", [])
        cart = context.user_data.get("cart", [])
        
        # Clear the pending item since we are abandoning it
        context.user_data.pop("pending_item", None)
        context.user_data.pop("problem_names", None)

        if not cart:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id,
                "Your cart is currently empty. Use /start to begin a new order.",
            )
            release_all(user_id)
            context.user_data.clear()
            return

        subtotal = _cart_subtotal(cart)
        total    = subtotal + SHIPPING_COST
        
        p_text = ", ".join(problem_names) if problem_names else "The item"

        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id,
            f"Final Review: Your updated order is shown below. {p_text} has been removed. Ready to proceed?\n\n"
            f"🛒 *Updated Cart:*\n{_cart_summary(cart)}\n\n"
            f"Subtotal: ${subtotal:.2f}\n"
            f"Shipping: ${SHIPPING_COST:.2f}\n"
            f"Total: ${total:.2f}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Complete My Checkout", callback_data="checkout")],
                [InlineKeyboardButton("Cancel order", callback_data="cancel_btn")],
            ]),
        )
        return

    # ── Payment: Square ───────────────────────────────────────────────────────
    if data == "pay_square":
        confirmed = context.user_data.get("confirmed_order")
        if not confirmed:
            await query.answer("Session expired. Please start over.", show_alert=True)
            return

        # Lock payment method immediately — disable buttons
        await query.edit_message_text(
            f"⏳ *Payment Method Locked: Square*\n\nGenerating your secure payment link...",
            parse_mode="Markdown",
        )

        username = update.effective_user.username
        customer = confirmed["customer"]
        total    = confirmed["subtotal"] + SHIPPING_COST


        # Create Square link
        result = create_square_link(confirmed["order_id"], confirmed["cart"], confirmed["subtotal"])

        if not result["url"]:
            await context.bot.send_message(
                chat_id,
                "⚠️ We encountered an issue generating your Square payment link. Please try again or choose a different payment method.\n\n"
                "Use /cancel to restart.",
                parse_mode="Markdown",
            )
            cancel_order_in_sheet(confirmed["order_id"])
            return

        # Store link details for cancellation
        context.user_data["payment_method"]  = "Square"
        context.user_data["square_link_id"]  = result["link_id"]
        context.user_data["step"]            = STEP_PAYMENT_WAIT

        _start_payment_timer(context, chat_id, user_id)

        await context.bot.send_message(
            chat_id,
            f"☑️ *Square Payment — Order #{confirmed['order_id']}*\n\n"
            f"💵 Total: *${total:.2f}* (incl. $20 shipping)\n\n"
            f"🔗 Click below to complete your payment:\n{result['url']}\n\n"
            f"📌 *Supports:* Credit/Debit Card, Apple Pay, Bitcoin\n\n"
            f"⏳ *You have {PAYMENT_TIMEOUT_MINUTES} minutes to complete payment.*\n"
            f"Click *✅ Payment Complete* below once done.\n\n"
            f"To switch payment methods, you must /cancel and restart.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Payment Complete", callback_data="payment_complete")],
                [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")]
            ]),
        )
        return

    # ── Payment: Stripe ───────────────────────────────────────────────────────
    if data == "pay_stripe":
        confirmed = context.user_data.get("confirmed_order")
        if not confirmed:
            await query.answer("Session expired. Please start over.", show_alert=True)
            return

        # Lock payment method immediately — disable buttons
        await query.edit_message_text(
            f"⏳ *Payment Method Locked: Stripe*\n\nGenerating your secure payment link...",
            parse_mode="Markdown",
        )

        username = update.effective_user.username
        customer = confirmed["customer"]
        total    = confirmed["subtotal"] + SHIPPING_COST
        result = create_stripe_session(confirmed["order_id"], confirmed["cart"], confirmed["subtotal"])

        if not result["url"]:
            await context.bot.send_message(
                chat_id,
                "⚠️ We encountered an issue generating your Stripe payment link. Please try again or choose a different payment method.\n\n"
                "Use /cancel to restart.",
                parse_mode="Markdown",
            )
            cancel_order_in_sheet(confirmed["order_id"])
            return

        context.user_data["payment_method"]    = "Stripe"
        context.user_data["stripe_session_id"] = result["session_id"]
        context.user_data["step"]              = STEP_PAYMENT_WAIT

        _start_payment_timer(context, chat_id, user_id)

        await context.bot.send_message(
            chat_id,
            f"✅ <b>Stripe Payment — Order #{confirmed['order_id']}</b>\n\n"
            f"💵 Total: <b>${total:.2f}</b> (incl. $20 shipping)\n\n"
            f"🔗 Click below to complete your payment:\n{result['url']}\n\n"
            f"📌 <b>Supports:</b> Credit/Debit Card, Google Pay, USDC Stablecoin\n\n"
            f"⏳ <b>You have {PAYMENT_TIMEOUT_MINUTES} minutes to complete payment.</b>\n"
            f"Click ✅ <b>Payment Complete</b> below once done.\n\n"
            f"To switch payment methods, you must /cancel and restart.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Payment Complete", callback_data="payment_complete")],
                [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")]
            ]),
        )
        return

    # ── Payment: Zelle ────────────────────────────────────────────────────────
    if data == "pay_zelle":
        confirmed = context.user_data.get("confirmed_order")
        if not confirmed:
            await query.answer("Session expired. Please start over.", show_alert=True)
            return

        # Lock payment method immediately — disable buttons
        await query.edit_message_text(
            "⏳ *Payment Method Locked: Zelle*\n\nPreparing your Zelle payment details...",
            parse_mode="Markdown",
        )

        username = update.effective_user.username
        customer = confirmed["customer"]
        total    = confirmed["subtotal"] + SHIPPING_COST



        context.user_data["payment_method"] = "Zelle"
        context.user_data["step"]           = STEP_PAYMENT_WAIT

        _start_payment_timer(context, chat_id, user_id)

        msg = get_zelle_message(
            confirmed["order_id"],
            confirmed["customer"],
            confirmed["cart"],
            confirmed["subtotal"],
        )
        await context.bot.send_message(
            chat_id,
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Payment Complete", callback_data="payment_complete")],
                [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")]
            ]),
        )
        return

    # ── Payment Complete (inline button) ──────────────────────────────────────
    if data == "payment_complete":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _handle_payment_complete(update, context)
        return

    # ── Cancel ────────────────────────────────────────────────────────────────
    # ── Cancel ────────────────────────────────────────────────────────────────
    if data == "cancel_btn":
        await _cancel_session(context, chat_id, user_id)
        return

# ── Address confirmation ───────────────────────────────────────────────────────
# ── Address confirmation ───────────────────────────────────────────────────────

async def _show_address_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    
    apt_line   = f", Unit {context.user_data['apt']}" if context.user_data.get("apt") else ""
    address    = f"{context.user_data['street']}{apt_line}, {context.user_data['city']}, {context.user_data['state']}, {context.user_data['zip']}"
    first_name = context.user_data.get("first_name", "Valued")
    last_name  = context.user_data.get("last_name", "Customer")
    cart       = context.user_data.get("cart", [])

    cart         = context.user_data.get("cart", [])
    raw_subtotal = _cart_subtotal(cart)
    
    promo_data   = context.user_data.get("promo_data")
    promo_disc   = 0.0
    promo_str    = ""
    
    # Calculate final discount based on whether it is percent or flat
    if promo_data:
        if promo_data["type"] == "percent":
            promo_disc = raw_subtotal * (promo_data["value"] / 100.0)
            pct = int(promo_data['value'])
            promo_str = f"🎫 <b>Promo Applied ({pct}%): -${promo_disc:.2f}</b>\n"
        else:
            promo_disc = promo_data["value"]
            promo_str = f"🎫 <b>Promo Applied: -${promo_disc:.2f}</b>\n"

    # Make sure we don't refund them if the flat code is larger than their cart!
    promo_disc = min(promo_disc, raw_subtotal)
    
    subtotal = raw_subtotal - promo_disc
    total    = subtotal + SHIPPING_COST


    # Create a line of text to show the discount (if they have one)
    promo_str = f"🎫 <b>Promo Applied: -${promo_disc:.2f}</b>\n" if promo_disc > 0 else ""

    # Safely convert the cart summary to HTML
    cart_summary_html = _cart_summary(cart).replace("<", "&lt;").replace(">", "&gt;")

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📋 <b>Final Review</b>\n\n"
            f"🧑 {first_name} {last_name}\n"
            f"🤳 {context.user_data['sms']}\n"
            f"📧 {context.user_data['email']}\n"
            f"🏠 {address}\n"
            f"🏙️  City: {context.user_data['city']}\n"
            f"🇺🇸 State: {context.user_data['state']}\n"
            f"📍 ZIP: {context.user_data['zip']}\n\n"
            f"🛒 <b>Your Cart:</b>\n{cart_summary_html}\n\n"
            f"    {promo_str}"
            f"    <b>Subtotal: ${subtotal:.2f}</b>\n"
            f"🚚 Shipping: ${SHIPPING_COST:.2f}\n"
            f"💰 <b>Total: ${total:.2f}</b>\n\n"
            f"🔹 <i>Type /cancel to exit this order</i>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Proceed to Order ✅", callback_data="addr_confirm")],
            [InlineKeyboardButton("✏️ Re-enter address",  callback_data="addr_redo")],
        ]),
    )
# ── Final review + order creation ─────────────────────────────────────────────
async def _proceed_to_final_review(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback: bool = False):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    cart         = context.user_data.get("cart", [])
    raw_subtotal = _cart_subtotal(cart)
    
    promo_data   = context.user_data.get("promo_data")
    promo_disc   = 0.0
    
    if promo_data:
        if promo_data["type"] == "percent":
            promo_disc = raw_subtotal * (promo_data["value"] / 100.0)
        else:
            promo_disc = promo_data["value"]

    promo_disc = min(promo_disc, raw_subtotal)
    final_subtotal = raw_subtotal - promo_disc
    order_id = _gen_order_id()

    customer = {
        "first_name": context.user_data.get("first_name", ""),
        "last_name":  context.user_data.get("last_name", ""),
        "sms":        context.user_data.get("sms", ""),
        "email":      context.user_data.get("email", ""),
        "street":     context.user_data.get("street", ""),
        "apt":        context.user_data.get("apt", ""),
        "city":       context.user_data.get("city", ""),
        "state":      context.user_data.get("state", ""),
        "zip":        context.user_data.get("zip", ""),
    }

    context.user_data["confirmed_order"] = {
        "order_id": order_id,
        "customer": customer,
        "cart":     cart,
        "subtotal": final_subtotal, # Save the discounted subtotal
    }
    context.user_data["step"] = STEP_PAYMENT

    if via_callback:
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass

    total = final_subtotal + SHIPPING_COST

    await context.bot.send_message(
        chat_id,
        f"✅ <b>Order #{order_id} Created!</b>\n\n"
        f"💵 Total: <b>${total:.2f}</b> (incl. $20 shipping)\n\n"
        f"Please select your preferred payment method.\n"
        f"⚠️ <b>Once selected, your choice is locked for {PAYMENT_TIMEOUT_MINUTES} minutes.</b>",
        parse_mode="HTML",
        reply_markup=_payment_keyboard(),
    )

# ── Payment complete ───────────────────────────────────────────────────────────

# ── Payment complete ───────────────────────────────────────────────────────────

# ── Payment complete ───────────────────────────────────────────────────────────

async def _handle_payment_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    chat_id   = update.effective_chat.id
    confirmed = context.user_data.get("confirmed_order")
    
    # Grab the payment method they used and their username
    pay_method = context.user_data.get("payment_method", "Unknown")
    username   = update.effective_user.username

    _stop_payment_timer(context, user_id)
    for job in context.job_queue.get_jobs_by_name(f"entry_timer_{user_id}"):
        job.schedule_removal()

    if not confirmed:
        await context.bot.send_message(chat_id, "No active order found. Use /start to begin.")
        return

    # 🟢 NEW: Log the order to Google Sheets ONLY AFTER the button is clicked!
    total = confirmed["subtotal"] + SHIPPING_COST
    
    for item in confirmed["cart"]:
            reduce_inventory(item["name"], item["qty"])

    release_all(user_id)
    
    # --- NEW: Generate readable timestamp (using Chicago time to match your cron jobs) ---
    tz = pytz.timezone("America/Chicago")
    current_timestamp = datetime.datetime.now(tz).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    
    log_new_order(
        order_id=confirmed["order_id"],
        chat_id=chat_id,
        username=f"@{username}" if username else "",
        customer=confirmed["customer"],
        cart=confirmed["cart"],
        final_price=total,
        payment_method=pay_method,
        transaction_datetime=current_timestamp,  # <-- Pushed to Column P
    )

    await context.bot.send_message(
        chat_id,
        f"🥳 *Congratulations 🎉 Order is Completed!!*\n"
        f"Your Order is now being processed ‼️\n\n"
        f"📧 Tracking numbers are sent *Monday-Friday*\n"
        f"🚚 Shipments normally arrive in *3 business days*\n\n"
        f"🕒 Orders completed Mon-Fri before *1PM EST* ship same day\n"
        f"🗓️ Orders *after 1PM EST* or on weekends ship the *next business day*\n\n"
        f"Thank You 💫\n"
        f"If you have any questions, just message @BioCivion 💬.\n"
        f"Press /start to start a new order.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Admin Order Alert ─────────────────────────────────────────────────────
    import threading
    threading.Thread(
        target=send_order_email, 
        args=(confirmed['order_id'], confirmed['customer'], pay_method, total)
    ).start()

    context.user_data.clear()


# ── Message handler ────────────────────────────────────────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    step    = context.user_data.get("step")

    # ── Payment Complete button (reply keyboard) ───────────────────────────────
    if text == "✅ Payment Complete":
        if step == STEP_PAYMENT:
            await update.message.reply_text(
                "⚠️ Please select your payment method above first!",
                reply_markup=_main_reply_keyboard(),
            )
            return
        await _handle_payment_complete(update, context)
        return

    # ── Block switching payment method if one is locked ────────────────────────
    if step == STEP_PAYMENT_WAIT:
        pay_method = context.user_data.get("payment_method", "")
        if pay_method:
            await update.message.reply_text(
                f"⚠️ *Action Denied:* You have already initiated a {pay_method} payment.\n\n"
                f"To switch to a different method, you must wait {PAYMENT_TIMEOUT_MINUTES} minutes "
                f"for the current order to expire, or type /cancel to restart.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Payment Complete", callback_data="payment_complete")]
                ]),
            )
        return

    # ── Cancel Order lookup ───────────────────────────────────────────────────
    if step == STEP_CANCEL_LOOKUP:
        order_id   = text.strip().upper().replace("#", "")
        order_data = find_order(order_id)

        if not order_data:
            await update.message.reply_text(f"❌ Order #{order_id} not found. Please try again or use /start.")
            context.user_data["step"] = STEP_MENU
            return

        context.user_data["pending_cancel_data"] = order_data
        await update.message.reply_text(
            f"⚠️ *Cancel Order Request*\n\n"
            f"*Order ID:* {order_data['order_id']}\n"
            f"*Items:* {order_data['items']}\n"
            f"*Total Paid:* {order_data['price']}\n\n"
            f"Are you sure you want to officially cancel this order?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Cancel Order", callback_data="confirm_cancel_yes")],
                [InlineKeyboardButton("❌ No, Keep Order",    callback_data="confirm_cancel_no")],
            ]),
        )
        context.user_data["step"] = STEP_MENU
        return

    # ── Cancel ID confirmation (mid-payment) ──────────────────────────────────
    if step == STEP_CANCEL_ID:
        confirmed = context.user_data.get("confirmed_order")
        if confirmed and text.strip().upper() == confirmed["order_id"].upper():
            await _cancel_session(context, chat_id, user_id, reason="manual")
        else:
            await update.message.reply_text("❌ Incorrect Order ID. Cancellation aborted.")
        return

    if not step:
        return
    
# ── Promo Code Entry ──────────────────────────────────────────────────────
    if step == "promo_enter":
        # Kill the 1-minute timer now that they typed something
        for job in context.job_queue.get_jobs_by_name(f"promo_timer_{user_id}"):
            job.schedule_removal()
            
        code = text.strip()
        
        # Check the Google Sheet!
        promo_data = check_promo_code(code)
        
        if promo_data:
            context.user_data["promo_code"] = code
            context.user_data["promo_data"] = promo_data  # Store the dict, not just a number
            
            cart = context.user_data.get("cart", [])
            subtotal = _cart_subtotal(cart)
            
            # Calculate the temporary discount just to show them a preview
            # Calculate the temporary discount just to show them a preview
            if promo_data["type"] == "percent":
                discount_amount = subtotal * (promo_data["value"] / 100.0)
                msg_text = f"✅ <b>Code applied! {promo_data['value']}% off.</b>"
            else:
                discount_amount = promo_data["value"]
                msg_text = f"✅ <b>Code applied! ${discount_amount:.2f} off.</b>"
                
            # Prevent discount from making subtotal negative
            discount_amount = min(discount_amount, subtotal)
            new_subtotal = subtotal - discount_amount
            
            await update.message.reply_text(
                f"{msg_text}\n\n<b>New Subtotal: ${new_subtotal:.2f}</b>",
                parse_mode="HTML",  # <--- MUST ADD THIS LINE
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Continue to Checkout 🛒", callback_data="skip_promo")],
                    [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_btn")]
                ])
            )
        else:
            # Code was invalid. Add +1 to their attempts.
            attempts = context.user_data.get("promo_attempts", 0) + 1
            context.user_data["promo_attempts"] = attempts
            
            if attempts < 2:
                await update.message.reply_text(
                    "Oops! That code doesn't seem to work. ❌\n"
                    "Select button below or Type '/cancel' to cancel order.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Try entering code more time 🔄", callback_data="retry_promo")],
                        [InlineKeyboardButton("Continue to payment 💸", callback_data="skip_promo")]
                    ])
                )
            else:
                await update.message.reply_text(
                    "Oops! That code doesn't seem to work. ❌\n"
                    "You've reached the maximum attempts for promo codes.\n"
                    "Type '/cancel' to cancel order or continue to payment.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Continue to payment 💸", callback_data="skip_promo")]
                    ])
                )
        return

    # ── Quantity entry ────────────────────────────────────────────────────────
    if step == STEP_QTY:
        pending = context.user_data.get("pending_item")
        if not pending:
            return

        if not text.isdigit():
            await update.message.reply_text(f"⚠️ Please enter a valid number between 1 and {pending['available']}.")
            return

        qty       = int(text)
        available = pending["available"]

        if qty < 1 or qty > available:
            await update.message.reply_text(
                f"Sorry, we only have {available} quantity. Please re-enter quantity amount.\n"
                f"You can also type '/cancel' to exit this order."
            )
            return

        item_name = pending["name"]
        stock     = pending["stock"]

        success, still_available = reserve(item_name, user_id, qty, stock)
        if not success:
            if still_available <= 0:
                context.user_data["problem_names"] = [item_name] # Store the name for the final review text
                await update.message.reply_text(
                    f"Oops! {item_name} is currently in high demand and being updated. Please wait a moment and try checking out again. To avoid a delay, would you like to continue your checkout without this item?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Yes, checkout without this item", callback_data="qty_checkout_without_item")],
                        [InlineKeyboardButton("❌ No, cancel my order", callback_data="cancel_btn")]
                    ])
                )
            else:
                # Update the pending available count so they can try a valid lower number
                context.user_data["pending_item"]["available"] = still_available
                await update.message.reply_text(
                    f"Sorry, we only have {still_available} quantity. Please re-enter quantity amount.\n"
                    f"You can also type '/cancel' to exit this order."
                )
            return

        cart = context.user_data.get("cart", [])
        original_price = pending["price"]

        existing = next((c for c in cart if c["name"] == item_name), None)
        if existing:
            new_qty           = existing["qty"] + qty
            existing["qty"]   = new_qty
            existing["price"] = original_price  # Keep original price consistent
            reserve(item_name, user_id, new_qty, stock)
        else:
            cart.append({
                "name":           item_name,
                "original_price": original_price,
                "price":          original_price, # Keep original price consistent
                "qty":            qty,
            })

        context.user_data["cart"] = cart
        context.user_data.pop("pending_item", None)
        context.user_data["step"] = STEP_MENU

        for job in context.job_queue.get_jobs_by_name(f"entry_timer_{user_id}"):
            job.schedule_removal()

        subtotal    = _cart_subtotal(cart)
        total       = subtotal + SHIPPING_COST
        summary     = _cart_summary(cart)
        
        # Calculate overall cart discount for the success message
        disc_pct    = int(_get_cart_discount_rate(cart) * 100)
        disc_notice = f"\n🏷️ *{disc_pct}% volume discount applied to your cart!*" if disc_pct > 0 else ""

        items_fresh      = get_items()


        success_keyboard = []
        for i in items_fresh:
            avail = get_available(i["name"], i["quantity"])
            if avail > 0:
                success_keyboard.append([InlineKeyboardButton(
                    f"{i['name']} ${i['price']:.2f} (qty: {avail})",
                    callback_data=f"buy_{i['name']}"
                )])
        success_keyboard.append([InlineKeyboardButton("✅ Proceed to Checkout", callback_data="checkout")])
        success_keyboard.append([InlineKeyboardButton("❌ Cancel Order",         callback_data="cancel_btn")])

        await update.message.reply_text(
            f"✅ *Added to cart!*{disc_notice}\n\n"
            f"🛒 *Your Cart:*\n{summary}\n\n"
            f"   *Subtotal: ${subtotal:.2f}*\n"
            f"🚚 Shipping: ${SHIPPING_COST:.2f}\n"
            f"💵 *Total: ${total:.2f}*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text(
            "Select another item or proceed to checkout:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(success_keyboard),
        )
        return

    # ── Info collection ───────────────────────────────────────────────────────
    _reset_entry_timer(context, chat_id, user_id)

    if step == STEP_EMAIL:
        if "@" not in text or "." not in text:
            await update.message.reply_text("⚠️ Please enter a valid email address.")
            return
        context.user_data["email"] = text
        context.user_data["step"]  = STEP_SMS
        await update.message.reply_text(
            "📱 *Step 2 of 8 — SMS Number*\n\nPlease enter your mobile number for delivery updates:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_SMS:
        context.user_data["sms"]  = text
        context.user_data["step"] = STEP_FIRST_NAME
        await update.message.reply_text(
            "👤 *Step 3 of 8 — First Name*\n\nPlease enter your first name:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_FIRST_NAME:
        context.user_data["first_name"] = text.title()
        context.user_data["step"]       = STEP_LAST_NAME
        await update.message.reply_text(
            "👤 *Step 4 of 8 — Last Name*\n\nPlease enter your last name:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_LAST_NAME:
        context.user_data["last_name"] = text.title()
        context.user_data["step"]      = STEP_STREET
        await update.message.reply_text(
            "📍 *Step 5 of 8 — Street Address*\n\nPlease enter your shipping street address *(no apartment/unit number)*:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_STREET:
        context.user_data["street"] = text.title()
        context.user_data["step"]   = STEP_APT
        await update.message.reply_text(
            "🏢 *Step 5b — Apartment/Unit Number*\n\nEnter your unit number, or tap *Skip* if none:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭️ Skip", callback_data="skip_apt")
            ]]),
        )
        return

    if step == STEP_APT:
        context.user_data["apt"]  = text
        context.user_data["step"] = STEP_CITY
        await update.message.reply_text(
            "🏙️ *Step 6 of 8 — City*\n\nPlease enter your city:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_CITY:
        context.user_data["city"] = text.title()
        context.user_data["step"] = STEP_STATE
        await update.message.reply_text(
            "🗺️ *Step 7 of 8 — State*\n\nPlease enter your 2-letter US state abbreviation (e.g. TX, CA, NY):\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_STATE:
        state = text.upper().strip()
        if state not in US_STATES:
            await update.message.reply_text("⚠️ Please enter a valid 2-letter US state abbreviation (e.g. TX, CA, FL).")
            return
        context.user_data["state"] = state
        context.user_data["step"]  = STEP_ZIP
        await update.message.reply_text(
            "📮 *Step 8 of 8 — ZIP Code*\n\nPlease enter your ZIP Code:\n\n_Type /cancel to exit this order_",
            parse_mode="Markdown",
        )
        return

    if step == STEP_ZIP:
        context.user_data["zip"]  = text.strip()
        context.user_data["step"] = STEP_CONFIRM_ADDR
        await _show_address_confirmation(update, context)
        return


# ── Background jobs ────────────────────────────────────────────────────────────

async def _cleanup_expired_orders(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs every 2 minutes. Finds orders past their payment expiry in the Sheet,
    cancels the payment link via API, marks sheet as Cancelled, notifies customer.
    """
    expired_orders = get_expired_pending_orders()
    for order in expired_orders:
        order_id   = order["order_id"]
        chat_id    = order["chat_id"]
        pay_method = order["payment_method"]

        # Note: we don't have link IDs here (they're in user_data, not the sheet).
        # For Square/Stripe, links expire naturally after 30 days.
        # The bot-side timer handles live sessions; this sheet cleanup is a safety net
        # for cases where the bot restarted and lost user_data.
        cancel_order_in_sheet(order_id)

        try:
            if chat_id:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"⌛ *Order #{order_id} Has Expired*\n\n"
                        f"Your {pay_method} payment window of {PAYMENT_TIMEOUT_MINUTES} minutes has passed "
                        f"and your order has been automatically cancelled.\n\n"
                        f"You may now type /start to begin a new order."
                    ),
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"Failed to notify user for expired order {order_id}: {e}")


async def _daily_tracking_check(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily at 9 PM CST to send tracking numbers."""
    pending_orders = get_pending_tracking()
    for order in pending_orders:
        try:
            await context.bot.send_message(
                chat_id=int(order["chat_id"]),
                text=(
                    f"🚚 *Great news!*\n\n"
                    f"Your order *#{order['order_id']}* has shipped!\n\n"
                    f"*Tracking Number:* `{order['tracking']}`"
                ),
                parse_mode="Markdown",
            )
            mark_notification_sent(order["row_index"])
        except Exception as e:
            logger.error(f"Failed to send tracking for {order['order_id']}: {e}")


async def _daily_verification_check(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily to send payment verification messages."""
    pending_orders = get_pending_verification()
    for order in pending_orders:
        try:
            await context.bot.send_message(
                chat_id=int(order["chat_id"]),
                text=(
                    f"✅ *Payment Verified!*\n\n"
                    f"Great news! We have successfully received and verified your payment "
                    f"for order *#{order['order_id']}*.\n\n"
                    f"📦 Your tracking number will be shared with you soon once it ships!"
                ),
                parse_mode="Markdown",
            )
            mark_verification_sent(order["row_index"])
        except Exception as e:
            logger.error(f"Failed to send verification for {order['order_id']}: {e}")

async def _daily_failed_check(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily to send 'Payment Not Verified' messages."""
    # We need to import our new function from sheets.py here
    from sheets import get_failed_verification, mark_verification_sent
    
    pending_orders = get_failed_verification()
    for order in pending_orders:
        try:
            await context.bot.send_message(
                chat_id=int(order["chat_id"]),
                text=(
                    f"⚠️ *Payment Not Verified*\n\n"
                    f"Unfortunately, we were unable to verify your payment for order *#{order['order_id']}*.\n\n"
                    f"Your order will not be processed. If you believe this is a mistake, please message @BioCivion."
                ),
                parse_mode="Markdown",
            )
            # This marks Column N as "Yes" so we don't spam them every day
            mark_verification_sent(order["row_index"])
        except Exception as e:
            logger.error(f"Failed to send failed verification for {order['order_id']}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
)
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("cancel",    cancel_command))
    app.add_handler(CommandHandler("inventory", inventory_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Schedule daily jobs (9 PM CST/CDT)
    tz       = pytz.timezone("America/Chicago")
    time_9pm = datetime.time(hour=21, minute=0, second=0, tzinfo=tz)
    app.job_queue.run_daily(_daily_tracking_check,     time=time_9pm)
    app.job_queue.run_daily(_daily_verification_check, time=time_9pm)
    app.job_queue.run_daily(_daily_failed_check,       time=time_9pm)

    # Cleanup expired orders every 2 minutes (sheet-level safety net)
    app.job_queue.run_repeating(_cleanup_expired_orders, interval=120, first=60)

    logger.info("BioCivion bot is running with Square + Stripe + Zelle payments...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()