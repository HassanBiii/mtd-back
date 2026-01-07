from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from queue import Queue
from datetime import datetime
import json

app = Flask(__name__)
CORS(app)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ----------------- Database -----------------
class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(10), nullable=False) # 'buy' or 'sell'
    quantity = db.Column(db.Float, nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    exit_price = db.Column(db.Float)
    commission = db.Column(db.Float, default=0.0)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id,
	    'symbol' : self.symbol,
            'side' : self.side,
            'quantity' : self.quantity,
            'entry_price' : self.entry_price,
            'exit_price' : self.exit_price,
            'commission' : self.commission,
            'opened_at' : self.opened_at,
            'closed_at' : self.closed_at
        }

    def is_open(self):
        return self.closed_at is None

    def realized_pnl(self):
        """
        محاسبه سود و زیان تحقق یافته
        """
        if self.closed_at and self.exit_price:
            diff = self.exit_price - self.entry_price
            
            # اگر معامله فروش (Short) بود، جهت تفاضل برعکس می‌شود
            if self.side == 'sell':
                diff = -1 * diff
                
            gross_pnl = diff * self.quantity
            return gross_pnl - self.commission
        return 0.0

# ----------------- SSE -----------------
subscribers = []

def notify_subscribers(event):
    for s in subscribers:
        s.put(event)

@app.route("/api/trade/stream")
def trade_stream():
    def gen():
        q = Queue()
        subscribers.append(q)
        try:
            while True:
                event = q.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            subscribers.remove(q)
    return Response(gen(), mimetype="text/event-stream")

# ----------------- Webhook POST -----------------
@app.route("/webhook/trade", methods=["POST"])
def webhook_trade():
    data = request.json

    # نگاشت فیلدهای جیسون جدید شما به متغیرها
    # JSON: symbol, action, price, qty, position_size, timenow
    try:
        symbol = data["symbol"]
        incoming_action = data["action"].lower() # 'buy' or 'sell'
        price = float(data["price"])
        qty = float(data["position_size"])
    except KeyError as e:
        return jsonify({"error": f"Missing field: {str(e)}"}), 400

    # بررسی اینکه آیا معامله باز برای این نماد داریم؟
    existing_trade = Trade.query.filter_by(symbol=symbol, closed_at=None).first()
    
    trade_event_type = "" # open or close
    current_trade = None

    if existing_trade:
        # اگر معامله باز داریم و سیگنال جدید مخالف جهت فعلی است (مثلا buy بود حالا sell اومده) -> بستن معامله
        if existing_trade.side != incoming_action:
            # 1. آپدیت کردن معامله باز قبلی (بستن آن)
            existing_trade.exit_price = price
            existing_trade.closed_at = datetime.utcnow()
            existing_trade.commission += data.get("commission", 0.0)
            
            # 2. (تغییر جدید) ذخیره کردن خودِ سیگنالِ بستن به عنوان یک رکورد جداگانه در دیتابیس
            # نکته: closed_at را همین الان پر می‌کنیم تا این رکورد به عنوان یک پوزیشن باز جدید تلقی نشود
            closing_log_trade = Trade(
                symbol=symbol,
                side=incoming_action, # مثلاً اگر قبلی buy بوده، این sell است
                quantity=qty,
                entry_price=price,    # قیمتی که در آن سیگنال بستن آمده
             #   exit_price=price,     # چون فقط لاگ است، خروج و ورود یکیست (یا میتواند نال باشد)
                commission=0.0,       # کمیسیون را روی ترید اصلی حساب کردیم
                opened_at=datetime.utcnow(),
              #  closed_at=datetime.utcnow() # مهم: بلافاصله بسته شده ثبت می‌شود
            )
            db.session.add(closing_log_trade)
            
            current_trade = existing_trade
            trade_event_type = "close"
            db.session.commit()
        else:
            # اگر جهت یکی بود (مثلا buy بود دوباره buy اومد) فعلا نادیده می‌گیریم
            return jsonify({"status": "ignored", "message": "Trade already open in same direction"}), 200
    else:
        # اگر معامله‌ای باز نیست -> ایجاد معامله جدید
        new_trade = Trade(
            symbol=symbol,
            side=incoming_action,
            quantity=qty,
            entry_price=price,
            commission=data.get("commission", 0.0)
        )
        db.session.add(new_trade)
        db.session.commit()
        
        current_trade = new_trade
        trade_event_type = "open"

    # آماده‌سازی ایونت برای ارسال به فرانت‌اند
    event = {
        "ts": datetime.utcnow().timestamp() * 1000,
        "trade_id": current_trade.id,
        "action": trade_event_type, # 'open' or 'close'
        "symbol": current_trade.symbol,
        "side": current_trade.side,
        "quantity": current_trade.quantity,
        "entry_price": current_trade.entry_price,
        "exit_price": current_trade.exit_price,
        "commission": current_trade.commission,
        "realized_pnl": current_trade.realized_pnl()
    }
    print(current_trade.realized_pnl())
    notify_subscribers(event)
    return jsonify({"status": "ok", "action": trade_event_type}), 200
# ----------------- Webhook GET-----------------
# return jsonify([trade.to_dict() for trade in trades])
@app.route("/webhook/trade", methods=["GET"])
def webhook_trade_get():
    trades = Trade.query.all()
    history = []
    for t in trades:
        # شبیه‌سازی فرمت ایونت SSE برای سوابق دیتابیس
        # اگر تاریخ بسته شدن دارد، اکشن را close در نظر می‌گیریم، وگرنه open
        action_status = "close" if t.closed_at else "open"
        
        # محاسبه PnL برای هر ردیف
        pnl = t.realized_pnl()
        
        history.append({
            "ts": t.opened_at.timestamp() * 1000,
            "trade_id": t.id,
            "action": action_status,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price if t.exit_price else 0.0,
            # در فرانت از فیلد price استفاده شده، آن را با قیمت خروج (یا ورود) پر می‌کنیم
            "price": t.exit_price if t.exit_price else t.entry_price,
            "commission": t.commission,
            "realized_pnl": pnl
        })
    return jsonify(history)
  	

# ----------------- Run -----------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    #app.run(debug=True, port=5000)
    app.run(host="0.0.0.0", port=8080)
