"""
Google Sheets integration.

Inventory Sheet layout (Row 1–4 = headers/metadata, data starts Row 5):
  A: Invoice Name | B: Peptide Name | C: Mg/Vial | D: Status | E: Qty (Single) | F: BioCivion price/vial

Orders Sheet columns (A–P):
  A:  Order ID
  B:  Telegram ID
  C:  Telegram Username
  D:  SMS Number
  E:  Customer Email
  F:  Items Summary
  G:  Final Price
  H:  Order Processing
  I:  Order Verified
  J:  Ready For Shipment
  K:  Order Shipped
  L:  Tracking Number
  M:  Notification Sent
  N:  Verification Sent
  O:  Payment Method        ← NEW (Square / Stripe / Zelle)
  P:  Payment Link Expiry   ← NEW (ISO timestamp string)
"""

import logging
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import SPREADSHEET_ID, SHEET_NAME, CREDENTIALS_FILE

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_service():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# ── Inventory ──────────────────────────────────────────────────────────────────

def get_items() -> list[dict]:
    """
    Fetch all in-stock and sold-out items from Google Sheets.
    Returns list of dicts: {name, description, price, quantity, row_index, ...}
    """
    try:
        service = _get_service()
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{SHEET_NAME}'!A5:F",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
        rows = result.get("values", [])
        items = []
        for i, row in enumerate(rows):
            while len(row) < 6:
                row.append("")
            invoice_name, peptide_name, mg_vial, status, qty_str, price_str = row[:6]

            if not peptide_name:
                continue

            try:
                qty   = int(float(qty_str)) if qty_str else 0
                price = float(str(price_str).replace("$", "").replace(",", "").strip()) if price_str else 0.0
            except ValueError:
                qty, price = 0, 0.0

            items.append({
                "invoice_name": invoice_name,
                "name":         f"{peptide_name} {mg_vial}".strip(),
                "peptide_name": peptide_name,
                "mg_vial":      mg_vial,
                "description":  f"{mg_vial} per vial",
                "price":        price,
                "quantity":     qty,
                "status":       status,
                "row_index":    i + 5,
            })
        return items
    except Exception as e:
        logger.error(f"Error fetching items from Sheets: {e}")
        return []


def reduce_inventory(item_name: str, qty: int = 1) -> bool:
    """Reduce quantity of item_name by qty. Returns True on success."""
    try:
        items = get_items()
        item  = next((i for i in items if i["name"] == item_name), None)
        if not item:
            logger.error(f"Item '{item_name}' not found.")
            return False
        if item["quantity"] < qty:
            logger.warning(f"Item '{item_name}' has insufficient stock.")
            return False

        new_qty = item["quantity"] - qty
        row     = item["row_index"]
        service = _get_service()

        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!E{row}",
            valueInputOption="RAW",
            body={"values": [[new_qty]]}
        ).execute()

        if new_qty <= 0:
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{SHEET_NAME}'!D{row}",
                valueInputOption="RAW",
                body={"values": [["SOLD OUT"]]}
            ).execute()

        logger.info(f"Reduced '{item_name}' by {qty} → {new_qty} remaining (row {row})")
        return True
    except Exception as e:
        logger.error(f"Error reducing inventory for '{item_name}': {e}")
        return False


# ── Orders ─────────────────────────────────────────────────────────────────────
def log_new_order(
    order_id: str,
    chat_id: int,
    username: str,
    customer: dict,  
    cart: list,
    final_price: float,
    payment_method: str = "",
    transaction_datetime: str = "", # <-- CHANGED THIS PARAMETER
):
    """
    Appends a completed order to the 'Orders' tab (23 columns A–W).
    """
    try:
        service       = _get_service()
        items_summary = ", ".join([f"{item['qty']}x {item['name']}" for item in cart])

        row_data = [
            order_id,
            str(chat_id),
            username if username else "No Username",
            customer.get("sms", ""),     # D: SMS
            customer.get("email", ""),   # E: Email
            items_summary,               # F: Items Summary
            f"${final_price:.2f}",       # G: Final Price
            "Yes",                       # H: Order Processing
            "",                          # I: Order Verified
            "",                          # J: Ready For Shipment
            "",                          # K: Order Shipped
            "",                          # L: Tracking Number
            "",                          # M: Notification Sent
            "",                          # N: Verification Sent
            payment_method,              # O: Payment Method
            transaction_datetime,        # P: Transaction Date & Time <-- CHANGED HERE
            customer.get("first_name", ""), # Q: First Name
            customer.get("last_name", ""),  # R: Last Name
            customer.get("street", ""),     # S: Street
            customer.get("apt", ""),        # T: Apt
            customer.get("city", ""),       # U: City
            customer.get("state", ""),      # V: State
            customer.get("zip", ""),        # W: ZIP
        ]

        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="'Orders'!A:W",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_data]}
        ).execute()

        logger.info(f"Order {order_id} logged to Sheets (method={payment_method}).")
    except Exception as e:
        logger.error(f"Failed to log order {order_id} to Sheets: {e}")

def find_order(order_id: str) -> dict:
    """Search the 'Orders' sheet for a specific Order ID."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:P"
        ).execute()

        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0].strip().upper() == order_id.strip().upper():
                while len(row) < 16:
                    row.append("")
                return {
                    "order_id":       row[0],
                    "chat_id":        row[1],
                    "email":          row[4],
                    "items":          row[5],
                    "price":          row[6],
                    "payment_method": row[14],
                    "payment_expiry": row[15],
                    "row_index":      i + 1,
                    "raw_row":        row,
                }
        return None
    except Exception as e:
        logger.error(f"Error finding order: {e}")
        return None


def set_payment_method(order_id: str, payment_method: str, expiry_iso: str) -> bool:
    """
    Write the payment method (col O) and expiry timestamp (col P)
    for an existing order row. Returns True on success.
    """
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:A"
        ).execute()

        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0].strip().upper() == order_id.strip().upper():
                sheet_row = i + 1
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"'Orders'!O{sheet_row}:P{sheet_row}",
                    valueInputOption="RAW",
                    body={"values": [[payment_method, expiry_iso]]}
                ).execute()
                logger.info(f"Set payment method '{payment_method}' for order {order_id} (row {sheet_row})")
                return True

        logger.warning(f"Order {order_id} not found when setting payment method.")
        return False
    except Exception as e:
        logger.error(f"Error setting payment method for {order_id}: {e}")
        return False


def get_payment_status(order_id: str) -> dict:
    """
    Returns {"payment_method": str, "payment_expiry": str, "row_index": int} for an order,
    or None if not found.
    """
    order = find_order(order_id)
    if not order:
        return None
    return {
        "payment_method": order.get("payment_method", ""),
        "payment_expiry": order.get("payment_expiry", ""),
        "row_index":      order.get("row_index", 0),
    }


def cancel_order_in_sheet(order_id: str) -> bool:
    """
    Updates the Orders sheet row for order_id:
    - Sets column H (Order Processing) to "Cancelled"
    """
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:A"
        ).execute()

        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0].strip().upper() == order_id.strip().upper():
                sheet_row = i + 1
                # Mark cancelled in col H
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"'Orders'!H{sheet_row}",
                    valueInputOption="RAW",
                    body={"values": [["Cancelled"]]}
                ).execute()
                
                # REMOVED the Column P clearing block so the transaction date stays permanently
                
                logger.info(f"Order {order_id} marked Cancelled in Sheets (row {sheet_row})")
                return True

        return False
    except Exception as e:
        logger.error(f"Error cancelling order {order_id} in Sheets: {e}")
        return False


def get_expired_pending_orders() -> list[dict]:
    """
    Returns orders where:
    - Col H (Order Processing) = "Yes"   (still pending)
    - Col P (Payment Link Expiry) is set AND the timestamp has passed
    Used by the cleanup job to auto-cancel expired orders.
    """
    import datetime
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:P"
        ).execute()

        rows    = result.get("values", [])
        now     = datetime.datetime.utcnow()
        expired = []

        for i, row in enumerate(rows):
            if i == 0:
                continue  # skip header
            while len(row) < 16:
                row.append("")

            order_id   = row[0]
            chat_id    = row[1]
            processing = row[7].strip().lower()
            pay_method = row[14].strip()
            expiry_str = row[15].strip()

            if processing != "yes" or not expiry_str or not order_id:
                continue

            try:
                expiry_dt = datetime.datetime.fromisoformat(expiry_str)
                if now >= expiry_dt:
                    expired.append({
                        "order_id":       order_id,
                        "chat_id":        chat_id,
                        "payment_method": pay_method,
                        "row_index":      i + 1,
                    })
            except ValueError:
                continue

        return expired
    except Exception as e:
        logger.error(f"Error fetching expired orders: {e}")
        return []


def log_cancelled_order(row_data: list):
    """Appends a cancelled order to the 'Cancel Order' tab."""
    try:
        service = _get_service()
        while len(row_data) < 16:
            row_data.append("")

        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="'Cancel Order'!A:P",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_data]}
        ).execute()
    except Exception as e:
        logger.error(f"Failed to log cancelled order to Sheets: {e}")


# ── Tracking notifications ─────────────────────────────────────────────────────

def get_pending_tracking() -> list[dict]:
    """Finds orders that have a tracking number but haven't been notified yet."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:M"
        ).execute()

        rows    = result.get("values", [])
        pending = []

        for i, row in enumerate(rows):
            if i == 0:
                continue
            while len(row) < 13:
                row.append("")

            order_id = row[0]
            chat_id  = row[1]
            tracking = row[11].strip()
            notified = row[12].strip()

            if tracking and not notified:
                pending.append({
                    "order_id":  order_id,
                    "chat_id":   chat_id,
                    "tracking":  tracking,
                    "row_index": i + 1,
                })
        return pending
    except Exception as e:
        logger.error(f"Error checking tracking: {e}")
        return []


def mark_notification_sent(row_index: int):
    """Marks column M (Notification Sent) as 'Yes'."""
    try:
        service = _get_service()
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'Orders'!M{row_index}",
            valueInputOption="RAW",
            body={"values": [["Yes"]]}
        ).execute()
    except Exception as e:
        logger.error(f"Error marking notification sent: {e}")


# ── Verification notifications ─────────────────────────────────────────────────

def get_pending_verification() -> list[dict]:
    """Finds orders that are verified but haven't been notified yet."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:N"
        ).execute()

        rows    = result.get("values", [])
        pending = []

        for i, row in enumerate(rows):
            if i == 0:
                continue
            while len(row) < 14:
                row.append("")

            order_id          = row[0]
            chat_id           = row[1]
            verified_status   = str(row[8]).strip().lower()
            verification_sent = str(row[13]).strip().lower()

            is_verified  = verified_status in ["yes", "verified", "true", "done"]
            already_sent = verification_sent in ["yes", "true", "done"]

            if is_verified and not already_sent and chat_id:
                pending.append({
                    "order_id":  order_id,
                    "chat_id":   chat_id,
                    "row_index": i + 1,
                })
        return pending
    except Exception as e:
        logger.error(f"Error checking verifications: {e}")
        return []


def mark_verification_sent(row_index: int):
    """Marks column N (Verification Sent) as 'Yes'."""
    try:
        service = _get_service()
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'Orders'!N{row_index}",
            valueInputOption="RAW",
            body={"values": [["Yes"]]}
        ).execute()
        logger.info(f"Marked verification sent for row {row_index}")
    except Exception as e:
        logger.error(f"Error marking verification sent: {e}")


def clear_payment_expiry(order_id: str) -> bool:
    """Clears column P (Payment Link Expiry) so the cleanup job skips completed orders."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:A"
        ).execute()

        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0].strip().upper() == order_id.strip().upper():
                sheet_row = i + 1
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"'Orders'!P{sheet_row}",
                    valueInputOption="RAW",
                    body={"values": [[""]]}  # Writes a blank cell to erase the timestamp
                ).execute()
                logger.info(f"Cleared expiry timestamp for completed order {order_id}")
                return True

        return False
    except Exception as e:
        logger.error(f"Error clearing expiry for {order_id} in Sheets: {e}")
        return False
    

def get_failed_verification() -> list[dict]:
    """Finds orders where Order Verified (Col I) is 'No' and Notification hasn't been sent."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:N"
        ).execute()

        rows    = result.get("values", [])
        pending = []

        for i, row in enumerate(rows):
            if i == 0:
                continue
            while len(row) < 14:
                row.append("")

            order_id          = row[0]
            chat_id           = row[1]
            verified_status   = str(row[8]).strip().lower()
            verification_sent = str(row[13]).strip().lower()

            # If you typed "no" in column I and we haven't notified them yet
            if verified_status == "no" and verification_sent != "yes" and chat_id:
                pending.append({
                    "order_id":  order_id,
                    "chat_id":   chat_id,
                    "row_index": i + 1,
                })
        return pending
    except Exception as e:
        logger.error(f"Error checking failed verifications: {e}")
        return []
    


def get_user_history(chat_id: str) -> dict:
    """Finds the most recent order for a chat_id and returns the customer details."""
    try:
        service = _get_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'Orders'!A:W"
        ).execute()

        rows = result.get("values", [])
        
        # Search backwards (bottom to top) to get their most recent order
        for row in reversed(rows):
            if len(row) > 1 and str(row[1]).strip() == str(chat_id).strip():
                # Ensure the row has all 23 columns
                while len(row) < 23:
                    row.append("")
                
                email = row[4].strip()
                if email:  # Only return if we actually saved an email previously
                    return {
                        "sms":        row[3].strip(),
                        "email":      email,
                        "first_name": row[16].strip(),
                        "last_name":  row[17].strip(),
                        "street":     row[18].strip(),
                        "apt":        row[19].strip(),
                        "city":       row[20].strip(),
                        "state":      row[21].strip(),
                        "zip":        row[22].strip(),
                    }
        return None
    except Exception as e:
        logger.error(f"Error fetching user history for {chat_id}: {e}")
        return None
    


##Promo Codes
def check_promo_code(code: str) -> dict:
    """
    Check PromoCodes sheet for the code. 
    Returns dict: {"type": "percent"|"flat", "value": float} or None if invalid.
    """
    try:
        service = _get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="'PromoCodes'!A:B" 
        ).execute()
        
        rows = result.get("values", [])
        for row in rows:
            if len(row) >= 2:
                sheet_code = str(row[0]).strip().lower()
                if sheet_code == code.strip().lower():
                    val_str = str(row[1]).strip()
                    try:
                        # If she included a % sign, treat it as a percentage
                        if "%" in val_str:
                            val = float(val_str.replace("%", "").strip())
                            return {"type": "percent", "value": val}
                        # Otherwise, treat it as a flat dollar amount
                        else:
                            val = float(val_str.replace("$", "").replace(",", "").strip())
                            return {"type": "flat", "value": val}
                    except ValueError:
                        return None
        return None
    except Exception as e:
        logger.error(f"Error checking promo code: {e}")
        return None