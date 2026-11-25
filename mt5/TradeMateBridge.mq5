//+------------------------------------------------------------------+
//| TradeMateBridge.mq5                                              |
//| Bridge between MetaTrader 5 and TradeMate (v0.4, DEMO-only).     |
//|                                                                  |
//| READ : writes a JSON snapshot (account, positions, symbols) to   |
//|        MT5's shared Common/Files folder about once a second.     |
//| WRITE: polls Common/Files for trademate_cmd_<id>.txt command     |
//|        files (open / modify / close / candles / history ...),     |
//|        executes them                                             |
//|        with the MT5 trade API and answers with                   |
//|        trademate_res_<id>.json containing the real broker retcode|
//|                                                                  |
//| Safety: refuses to trade unless the account is a DEMO account,   |
//| refuses expired commands, and only does what a command says.     |
//| It never trades on its own and has no strategy.                  |
//+------------------------------------------------------------------+
#property copyright "Helixa Technologies"
#property version   "4.00"
#property description "Snapshot + manual DEMO execution bridge for TradeMate. No automatic trading."

#include <Trade\Trade.mqh>

input int    UpdateIntervalMs = 1000;                    // How often to write the snapshot (ms)
input int    MaxSymbols       = 30;                      // Max Market Watch symbols in the snapshot (watchlist + open-position symbols are always added)
input int    MaxTracked       = 100;                     // Max watchlist symbols followed
input string WatchFile        = "trademate_watch.txt";   // Written by TradeMate: the symbols you added
input string SnapshotFile     = "trademate_snapshot.json"; // Must match [mt5] snapshot_file in TradeMate
input int    CommandPollMs    = 200;                     // How often to look for TradeMate commands (ms)
input bool   AllowTrading     = true;                    // Master switch. false = read-only bridge
input int    MaxCandles       = 500;                     // Max candles returned for a chart request

#define BRIDGE_VERSION "0.5.0"
#define MAX_HISTORY    500                      // Max closed trades returned for a history request
#define SCHEMA_VERSION 2

datetime g_started_gmt = 0;
string   g_tmp_file    = "";
bool     g_write_error_reported = false;
uint     g_last_snapshot_ms = 0;
CTrade   g_trade;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_started_gmt = TimeGMT();
   g_tmp_file    = SnapshotFile + ".tmp";

   int interval = CommandPollMs;
   if(interval < 100)
      interval = 100;
   g_trade.SetAsyncMode(false);   // synchronous: we wait for the broker's answer

   if(!EventSetMillisecondTimer(interval))
     {
      Print("TradeMateBridge: could not start the timer. Error ", GetLastError());
      return(INIT_FAILED);
     }

   Print("TradeMateBridge ", BRIDGE_VERSION, " started. Trading ", (AllowTrading ? "ENABLED (DEMO accounts only)" : "DISABLED"),
         ". Snapshot every ", UpdateIntervalMs, " ms, command poll every ", interval, " ms.");
   WriteSnapshot();
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   Print("TradeMateBridge stopped.");
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   ProcessCommands();
   uint now = GetTickCount();
   if(g_last_snapshot_ms == 0 || (now - g_last_snapshot_ms) >= (uint)UpdateIntervalMs)
     {
      g_last_snapshot_ms = now;
      WriteSnapshot();
     }
  }

//+------------------------------------------------------------------+
//| JSON helpers                                                      |
//+------------------------------------------------------------------+
string JsonStr(const string text)
  {
   string out = "\"";
   int n = StringLen(text);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(text, i);
      if(c == '"')
         out += "\\\"";
      else if(c == '\\')
         out += "\\\\";
      else if(c == '\n')
         out += "\\n";
      else if(c == '\r')
         out += "\\r";
      else if(c == '\t')
         out += "\\t";
      else if(c < 32)
         out += " ";
      else
         out += ShortToString(c);
     }
   out += "\"";
   return(out);
  }

string JsonNum(const double value, const int digits)
  {
   if(!MathIsValidNumber(value))
      return("null");
   return(DoubleToString(value, digits));
  }

// SL/TP of 0 means "not set"
string JsonNumOrNull(const double value, const int digits)
  {
   if(value == 0.0)
      return("null");
   return(JsonNum(value, digits));
  }

string JsonBool(const bool value)
  {
   return(value ? "true" : "false");
  }

string TradeModeName(const long mode)
  {
   if(mode == ACCOUNT_TRADE_MODE_DEMO)
      return("demo");
   if(mode == ACCOUNT_TRADE_MODE_CONTEST)
      return("contest");
   if(mode == ACCOUNT_TRADE_MODE_REAL)
      return("real");
   return("unknown");
  }

//+------------------------------------------------------------------+
//| Snapshot sections                                                 |
//+------------------------------------------------------------------+
string BuildTerminal()
  {
   string s = "{";
   s += "\"connected\":" + JsonBool((bool)TerminalInfoInteger(TERMINAL_CONNECTED));
   s += ",\"name\":" + JsonStr(TerminalInfoString(TERMINAL_NAME));
   s += ",\"company\":" + JsonStr(TerminalInfoString(TERMINAL_COMPANY));
   s += ",\"build\":" + IntegerToString((long)TerminalInfoInteger(TERMINAL_BUILD));
   s += "}";
   return(s);
  }

string BuildAccount()
  {
   string s = "{";
   s += "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   s += ",\"server\":" + JsonStr(AccountInfoString(ACCOUNT_SERVER));
   s += ",\"company\":" + JsonStr(AccountInfoString(ACCOUNT_COMPANY));
   s += ",\"currency\":" + JsonStr(AccountInfoString(ACCOUNT_CURRENCY));
   s += ",\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE));
   s += ",\"trade_mode\":" + JsonStr(TradeModeName(AccountInfoInteger(ACCOUNT_TRADE_MODE)));
   s += ",\"balance\":" + JsonNum(AccountInfoDouble(ACCOUNT_BALANCE), 2);
   s += ",\"equity\":" + JsonNum(AccountInfoDouble(ACCOUNT_EQUITY), 2);
   s += ",\"margin\":" + JsonNum(AccountInfoDouble(ACCOUNT_MARGIN), 2);
   s += ",\"free_margin\":" + JsonNum(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   s += ",\"margin_level\":" + JsonNum(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2);
   s += ",\"profit\":" + JsonNum(AccountInfoDouble(ACCOUNT_PROFIT), 2);
   s += "}";
   return(s);
  }

string BuildPositions()
  {
   string s = "[";
   int total = PositionsTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);   // selects the position for the Get calls below
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long ptype = PositionGetInteger(POSITION_TYPE);

      if(!first)
         s += ",";
      first = false;

      s += "{";
      s += "\"ticket\":" + IntegerToString((long)ticket);
      s += ",\"symbol\":" + JsonStr(symbol);
      s += ",\"type\":" + JsonStr(ptype == POSITION_TYPE_BUY ? "buy" : "sell");
      s += ",\"volume\":" + JsonNum(PositionGetDouble(POSITION_VOLUME), 4);
      s += ",\"price_open\":" + JsonNum(PositionGetDouble(POSITION_PRICE_OPEN), digits);
      s += ",\"price_current\":" + JsonNum(PositionGetDouble(POSITION_PRICE_CURRENT), digits);
      s += ",\"sl\":" + JsonNumOrNull(PositionGetDouble(POSITION_SL), digits);
      s += ",\"tp\":" + JsonNumOrNull(PositionGetDouble(POSITION_TP), digits);
      s += ",\"profit\":" + JsonNum(PositionGetDouble(POSITION_PROFIT), 2);
      s += ",\"swap\":" + JsonNum(PositionGetDouble(POSITION_SWAP), 2);
      s += "}";
     }
   s += "]";
   return(s);
  }

//+------------------------------------------------------------------+
//| Pending orders (0.5.0). Supported: buy/sell limit and buy/sell    |
//| stop. Stop-limit orders are deliberately NOT supported.           |
//+------------------------------------------------------------------+
// MT5 order times are BROKER SERVER time. TradeMate speaks UTC epoch seconds, so convert at the edge in both directions.
long ServerOffsetSeconds()
  {
   return((long)(TimeTradeServer() - TimeGMT()));
  }

string PendingTypeName(const long t)
  {
   if(t == ORDER_TYPE_BUY_LIMIT)
      return("buy_limit");
   if(t == ORDER_TYPE_SELL_LIMIT)
      return("sell_limit");
   if(t == ORDER_TYPE_BUY_STOP)
      return("buy_stop");
   if(t == ORDER_TYPE_SELL_STOP)
      return("sell_stop");
   if(t == ORDER_TYPE_BUY_STOP_LIMIT)
      return("buy_stop_limit");     // reported by name so TradeMate can ignore it explicitly
   if(t == ORDER_TYPE_SELL_STOP_LIMIT)
      return("sell_stop_limit");
   return("other");
  }

// -1 = not a supported pending type
int PendingTypeFromName(const string name)
  {
   if(name == "buy_limit")
      return(ORDER_TYPE_BUY_LIMIT);
   if(name == "sell_limit")
      return(ORDER_TYPE_SELL_LIMIT);
   if(name == "buy_stop")
      return(ORDER_TYPE_BUY_STOP);
   if(name == "sell_stop")
      return(ORDER_TYPE_SELL_STOP);
   return(-1);
  }

string BuildOrders()
  {
   string s = "[";
   int total = OrdersTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = OrderGetTicket(i);       // selects the order for the Get calls below
      if(ticket == 0)
         continue;
      string symbol = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      if(!first)
         s += ",";
      first = false;
      s += "{";
      s += "\"ticket\":" + IntegerToString((long)ticket);
      s += ",\"symbol\":" + JsonStr(symbol);
      s += ",\"type\":" + JsonStr(PendingTypeName(OrderGetInteger(ORDER_TYPE)));
      s += ",\"volume\":" + JsonNum(OrderGetDouble(ORDER_VOLUME_CURRENT), 4);
      s += ",\"price_open\":" + JsonNum(OrderGetDouble(ORDER_PRICE_OPEN), digits);
      s += ",\"sl\":" + JsonNumOrNull(OrderGetDouble(ORDER_SL), digits);
      s += ",\"tp\":" + JsonNumOrNull(OrderGetDouble(ORDER_TP), digits);
      long exp_server = (long)OrderGetInteger(ORDER_TIME_EXPIRATION);
      s += ",\"expiration\":" + (exp_server > 0 ? IntegerToString(exp_server - ServerOffsetSeconds()) : "null");    // UTC epoch
      s += ",\"time_setup\":" + IntegerToString((long)OrderGetInteger(ORDER_TIME_SETUP) - ServerOffsetSeconds());
      s += "}";
     }
   s += "]";
   return(s);
  }

//+------------------------------------------------------------------+
//| One symbol as JSON. detail=true adds the slower-changing fields   |
//| (path, tick size/value, margin currency); the snapshot omits them |
//| to stay small, on-demand requests include them.                   |
//+------------------------------------------------------------------+
string BuildSymbolJson(const string name, const bool detail)
  {
   int digits = (int)SymbolInfoInteger(name, SYMBOL_DIGITS);
   MqlTick tick;
   bool have_tick = SymbolInfoTick(name, tick) && (tick.bid > 0.0 || tick.ask > 0.0);

   string s = "{";
   s += "\"name\":" + JsonStr(name);
   s += ",\"description\":" + JsonStr(SymbolInfoString(name, SYMBOL_DESCRIPTION));
   s += ",\"digits\":" + IntegerToString(digits);
   s += ",\"point\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_POINT), 10);
   s += ",\"spread\":" + IntegerToString(SymbolInfoInteger(name, SYMBOL_SPREAD));
   s += ",\"contract_size\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_TRADE_CONTRACT_SIZE), 2);
   s += ",\"volume_min\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_VOLUME_MIN), 4);
   s += ",\"volume_max\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_VOLUME_MAX), 4);
   s += ",\"volume_step\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_VOLUME_STEP), 4);
   s += ",\"stops_level\":" + IntegerToString(SymbolInfoInteger(name, SYMBOL_TRADE_STOPS_LEVEL));
   s += ",\"freeze_level\":" + IntegerToString(SymbolInfoInteger(name, SYMBOL_TRADE_FREEZE_LEVEL));
   s += ",\"trade_mode\":" + IntegerToString(SymbolInfoInteger(name, SYMBOL_TRADE_MODE));
   s += ",\"currency_base\":" + JsonStr(SymbolInfoString(name, SYMBOL_CURRENCY_BASE));
   s += ",\"currency_profit\":" + JsonStr(SymbolInfoString(name, SYMBOL_CURRENCY_PROFIT));
   s += ",\"bid\":" + (have_tick ? JsonNum(tick.bid, digits) : "null");
   s += ",\"ask\":" + (have_tick ? JsonNum(tick.ask, digits) : "null");
   if(detail)
     {
      s += ",\"path\":" + JsonStr(SymbolInfoString(name, SYMBOL_PATH));
      s += ",\"currency_margin\":" + JsonStr(SymbolInfoString(name, SYMBOL_CURRENCY_MARGIN));
      s += ",\"tick_size\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_SIZE), 10);
      s += ",\"tick_value\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE), 6);
      s += ",\"tick_value_profit\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_PROFIT), 6);
      s += ",\"tick_value_loss\":" + JsonNum(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_LOSS), 6);
      s += ",\"visible\":" + JsonBool((bool)SymbolInfoInteger(name, SYMBOL_SELECT));
     }
   s += "}";
   return(s);
  }

bool InList(const string &list[], const string name)
  {
   for(int i = 0; i < ArraySize(list); i++)
      if(list[i] == name)
         return(true);
   return(false);
  }

void AddUnique(string &list[], const string name)
  {
   if(name == "" || InList(list, name))
      return;
   int n = ArraySize(list);
   ArrayResize(list, n + 1);
   list[n] = name;
  }

//+------------------------------------------------------------------+
//| Symbols TradeMate asked to follow: trademate_watch.txt (one name  |
//| per line, written by TradeMate). Missing file = no extra symbols. |
//+------------------------------------------------------------------+
void ReadWatchFile(string &out[])
  {
   int h = FileOpen(WatchFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE, 0, CP_UTF8);
   if(h == INVALID_HANDLE)
      return;
   while(!FileIsEnding(h) && ArraySize(out) < MaxTracked)
     {
      string line = FileReadString(h);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line != "")
         AddUnique(out, line);
     }
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| What the snapshot exports (efficient even with 1000s of broker    |
//| symbols - the full catalogue is only searched on demand):         |
//|   1. symbols with open positions   (always, so P/L context exists)|
//|   2. symbols on TradeMate's watchlist                             |
//|   3. Market Watch symbols, up to MaxSymbols                       |
//+------------------------------------------------------------------+
string BuildSymbols()
  {
   string names[];
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0)
         AddUnique(names, PositionGetString(POSITION_SYMBOL));
     }
   string watch[];
   ReadWatchFile(watch);
   for(int i = 0; i < ArraySize(watch); i++)
     {
      // Make sure ticks flow for it. SymbolSelect is cheap when already selected.
      if(!(bool)SymbolInfoInteger(watch[i], SYMBOL_SELECT))
         if(!SymbolSelect(watch[i], true))
            continue;                      // not a symbol of this broker: skip
      AddUnique(names, watch[i]);
     }
   int mw = SymbolsTotal(true);
   if(mw > MaxSymbols)
      mw = MaxSymbols;
   for(int i = 0; i < mw; i++)
      AddUnique(names, SymbolName(i, true));

   string s = "[";
   bool first = true;
   for(int i = 0; i < ArraySize(names); i++)
     {
      if(!first)
         s += ",";
      first = false;
      s += BuildSymbolJson(names[i], false);
     }
   s += "]";
   return(s);
  }

string BuildSnapshot()
  {
   long uptime = (long)(TimeGMT() - g_started_gmt);

   string s = "{";
   s += "\"schema\":" + IntegerToString(SCHEMA_VERSION);
   s += ",\"written_at_gmt\":" + IntegerToString((long)TimeGMT());
   s += ",\"bridge\":{\"name\":\"TradeMateBridge\",\"version\":\"" + BRIDGE_VERSION + "\",\"read_only\":" + JsonBool(!AllowTrading) + ",\"trading_enabled\":" + JsonBool(AllowTrading && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED) && (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) + ",\"uptime_seconds\":" + IntegerToString(uptime) + ",\"features\":[\"pending_orders\"]}";
   s += ",\"terminal\":" + BuildTerminal();
   s += ",\"account\":" + BuildAccount();
   s += ",\"positions\":" + BuildPositions();
   s += ",\"orders\":" + BuildOrders();
   s += ",\"symbols\":" + BuildSymbols();
   s += "}";
   return(s);
  }

//+------------------------------------------------------------------+
//| Write to a temp file, then replace the real file in one step so  |
//| TradeMate never reads a half-written snapshot.                   |
//+------------------------------------------------------------------+
void WriteSnapshot()
  {
   string json = BuildSnapshot();

   ResetLastError();
   int handle = FileOpen(g_tmp_file, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      if(!g_write_error_reported)
         Print("TradeMateBridge: cannot open ", g_tmp_file, " for writing. Error ", GetLastError());
      g_write_error_reported = true;
      return;
     }
   FileWriteString(handle, json);
   FileClose(handle);

   ResetLastError();
   if(!FileMove(g_tmp_file, FILE_COMMON, SnapshotFile, FILE_REWRITE | FILE_COMMON))
     {
      if(!g_write_error_reported)
         Print("TradeMateBridge: cannot replace ", SnapshotFile, ". Error ", GetLastError());
      g_write_error_reported = true;
      return;
     }
   g_write_error_reported = false;
  }

//+------------------------------------------------------------------+
//| COMMAND EXECUTION                                                 |
//| Command file (key=value lines):  trademate_cmd_<id>.txt           |
//| Result file (JSON):              trademate_res_<id>.json          |
//+------------------------------------------------------------------+
string CmdGet(string &keys[], string &vals[], const string key)
  {
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key)
         return(vals[i]);
   return("");
  }

ENUM_TIMEFRAMES TfFromName(const string tf)
  {
   if(tf == "M1")  return(PERIOD_M1);
   if(tf == "M5")  return(PERIOD_M5);
   if(tf == "M15") return(PERIOD_M15);
   if(tf == "M2") return(PERIOD_M2);
   if(tf == "M30") return(PERIOD_M30);
   if(tf == "H1")  return(PERIOD_H1);
   if(tf == "H4")  return(PERIOD_H4);
   if(tf == "D1")  return(PERIOD_D1);
   return(PERIOD_H1);
  }

void WriteResult(const string id, const string json)
  {
   string tmp  = "trademate_res_" + id + ".tmp";
   string name = "trademate_res_" + id + ".json";
   int h = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON, 0, CP_UTF8);
   if(h == INVALID_HANDLE)
     {
      Print("TradeMateBridge: cannot write result ", name, " error ", GetLastError());
      return;
     }
   FileWriteString(h, json);
   FileClose(h);
   FileMove(tmp, FILE_COMMON, name, FILE_REWRITE | FILE_COMMON);
  }

string ResultJson(const string id, const bool ok, const uint retcode, const string message,
                  const ulong ticket, const double price, const string extra = "")
  {
   string s = "{\"id\":" + JsonStr(id);
   s += ",\"ok\":" + JsonBool(ok);
   s += ",\"retcode\":" + IntegerToString((long)retcode);
   s += ",\"message\":" + JsonStr(message);
   s += ",\"ticket\":" + IntegerToString((long)ticket);
   s += ",\"price\":" + JsonNum(price, 8);
   if(extra != "")
      s += "," + extra;
   s += "}";
   return(s);
  }

bool RetcodeOk(const uint rc)
  {
   return(rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_DONE_PARTIAL || rc == TRADE_RETCODE_PLACED);
  }

// Normalise a price to the symbol's digits (0 stays 0 = "no SL/TP").
double Norm(const string symbol, const double price)
  {
   if(price <= 0.0)
      return(0.0);
   return(NormalizeDouble(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
  }

void DoCandles(const string id, string &keys[], string &vals[])
  {
   string symbol = CmdGet(keys, vals, "symbol");
   ENUM_TIMEFRAMES tf = TfFromName(CmdGet(keys, vals, "timeframe"));
   int count = (int)StringToInteger(CmdGet(keys, vals, "count"));
   if(count < 10)
      count = 10;
   if(count > MaxCandles)
      count = MaxCandles;
   if(!SymbolSelect(symbol, true))
     {
      WriteResult(id, ResultJson(id, false, 0, "Symbol '" + symbol + "' is not available on this broker.", 0, 0));
      return;
     }
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int got = CopyRates(symbol, tf, 0, count, rates);
   if(got <= 0)
     {
      WriteResult(id, ResultJson(id, false, 0, "No candles yet for " + symbol + " (history still loading, try again).", 0, 0));
      return;
     }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   string bars = "\"bars\":[";
   for(int i = 0; i < got; i++)
     {
      if(i > 0)
         bars += ",";
      bars += "[" + IntegerToString((long)rates[i].time) + "," + DoubleToString(rates[i].open, digits) + "," +
              DoubleToString(rates[i].high, digits) + "," + DoubleToString(rates[i].low, digits) + "," +
              DoubleToString(rates[i].close, digits) + "]";
     }
   bars += "]";
   WriteResult(id, ResultJson(id, true, 0, "ok", 0, 0, bars));
  }

// Wait briefly for the first tick of a freshly selected symbol.
void WaitForTick(const string symbol)
  {
   for(int i = 0; i < 10; i++)
     {
      MqlTick tick;
      if(SymbolInfoTick(symbol, tick) && (tick.bid > 0.0 || tick.ask > 0.0))
         return;
      Sleep(100);
     }
  }

// symbol_info: select the symbol on demand and return everything we know about it.
void DoSymbolInfo(const string id, string &keys[], string &vals[])
  {
   string symbol = CmdGet(keys, vals, "symbol");
   if(symbol == "" || !SymbolSelect(symbol, true))
     {
      WriteResult(id, ResultJson(id, false, 0, "Symbol '" + symbol + "' does not exist at this broker.", 0, 0));
      return;
     }
   WaitForTick(symbol);
   WriteResult(id, ResultJson(id, true, 0, "ok", 0, 0, "\"symbol\":" + BuildSymbolJson(symbol, true)));
  }

// symbols_search: look through the broker's WHOLE catalogue (not only Market Watch).
// Matches name, description or path (case-insensitive). Names that start with the query come first.
void DoSymbolsSearch(const string id, string &keys[], string &vals[])
  {
   string q = CmdGet(keys, vals, "query");
   StringToLower(q);
   int limit = (int)StringToInteger(CmdGet(keys, vals, "limit"));
   if(limit < 1)
      limit = 40;
   if(limit > 200)
      limit = 200;

   int total = SymbolsTotal(false);
   string starts[], contains[];
   int matches = 0;
   for(int i = 0; i < total; i++)
     {
      string name = SymbolName(i, false);
      if(name == "")
         continue;
      if(q == "")
        {
         matches++;
         if(ArraySize(contains) < limit)
            AddUnique(contains, name);
         continue;
        }
      string lname = name;
      StringToLower(lname);
      int pos = StringFind(lname, q);
      if(pos == 0)
        {
         matches++;
         if(ArraySize(starts) < limit)
            AddUnique(starts, name);
         continue;
        }
      bool hit = (pos > 0);
      if(!hit)
        {
         string d = SymbolInfoString(name, SYMBOL_DESCRIPTION);
         StringToLower(d);
         hit = (StringFind(d, q) >= 0);
        }
      if(!hit)
        {
         string p = SymbolInfoString(name, SYMBOL_PATH);
         StringToLower(p);
         hit = (StringFind(p, q) >= 0);
        }
      if(hit)
        {
         matches++;
         if(ArraySize(contains) < limit)
            AddUnique(contains, name);
        }
     }

   string out = "";
   int written = 0;
   for(int pass = 0; pass < 2 && written < limit; pass++)
     {
      int n = (pass == 0 ? ArraySize(starts) : ArraySize(contains));
      for(int i = 0; i < n && written < limit; i++)
        {
         string nm = (pass == 0 ? starts[i] : contains[i]);
         if(written > 0)
            out += ",";
         out += "{\"name\":" + JsonStr(nm);
         out += ",\"description\":" + JsonStr(SymbolInfoString(nm, SYMBOL_DESCRIPTION));
         out += ",\"path\":" + JsonStr(SymbolInfoString(nm, SYMBOL_PATH));
         out += ",\"trade_mode\":" + IntegerToString(SymbolInfoInteger(nm, SYMBOL_TRADE_MODE));
         out += ",\"visible\":" + JsonBool((bool)SymbolInfoInteger(nm, SYMBOL_SELECT)) + "}";
         written++;
        }
     }
   string extra = "\"matches\":" + IntegerToString(matches) + ",\"total_symbols\":" + IntegerToString(total) +
                  ",\"found\":[" + out + "]";
   WriteResult(id, ResultJson(id, true, 0, "ok", 0, 0, extra));
  }

void DoOpen(const string id, string &keys[], string &vals[])
  {
   string symbol = CmdGet(keys, vals, "symbol");
   string side   = CmdGet(keys, vals, "side");
   double volume = StringToDouble(CmdGet(keys, vals, "volume"));
   double sl     = StringToDouble(CmdGet(keys, vals, "sl"));
   double tp     = StringToDouble(CmdGet(keys, vals, "tp"));
   int deviation = (int)StringToInteger(CmdGet(keys, vals, "deviation"));
   long magic    = StringToInteger(CmdGet(keys, vals, "magic"));
   if(deviation <= 0)
      deviation = 20;

   if(!SymbolSelect(symbol, true))
     {
      WriteResult(id, ResultJson(id, false, 0, "Symbol '" + symbol + "' is not available on this broker.", 0, 0));
      return;
     }
   g_trade.SetExpertMagicNumber((ulong)magic);
   g_trade.SetDeviationInPoints(deviation);
   g_trade.SetTypeFillingBySymbol(symbol);

   ResetLastError();
   bool sent = false;
   if(side == "buy")
      sent = g_trade.Buy(volume, symbol, 0.0, Norm(symbol, sl), Norm(symbol, tp), "TradeMate");
   else if(side == "sell")
      sent = g_trade.Sell(volume, symbol, 0.0, Norm(symbol, sl), Norm(symbol, tp), "TradeMate");
   else
     {
      WriteResult(id, ResultJson(id, false, 0, "Unknown side '" + side + "'.", 0, 0));
      return;
     }

   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   bool ok = (sent && RetcodeOk(rc));

   // Find the position ticket created by this deal.
   ulong pos_ticket = 0;
   ulong deal = g_trade.ResultDeal();
   if(ok && deal > 0 && HistoryDealSelect(deal))
      pos_ticket = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   if(ok && pos_ticket == 0)
      pos_ticket = g_trade.ResultOrder();

   Print("TradeMateBridge OPEN ", side, " ", volume, " ", symbol, " -> retcode ", rc, " ", desc, " ticket ", pos_ticket);
   WriteResult(id, ResultJson(id, ok, rc, desc, pos_ticket, g_trade.ResultPrice()));
  }

void DoModify(const string id, string &keys[], string &vals[])
  {
   ulong ticket = (ulong)StringToInteger(CmdGet(keys, vals, "ticket"));
   double sl = StringToDouble(CmdGet(keys, vals, "sl"));
   double tp = StringToDouble(CmdGet(keys, vals, "tp"));
   if(!PositionSelectByTicket(ticket))
     {
      WriteResult(id, ResultJson(id, false, 0, "Position " + IntegerToString((long)ticket) + " is not open (already closed?).", ticket, 0));
      return;
     }
   string symbol = PositionGetString(POSITION_SYMBOL);
   ResetLastError();
   bool sent = g_trade.PositionModify(ticket, Norm(symbol, sl), Norm(symbol, tp));
   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   // "no changes" means the broker already holds exactly these values.
   bool ok = (sent && RetcodeOk(rc)) || rc == TRADE_RETCODE_NO_CHANGES;
   Print("TradeMateBridge MODIFY #", ticket, " sl ", sl, " tp ", tp, " -> retcode ", rc, " ", desc);
   WriteResult(id, ResultJson(id, ok, rc, desc, ticket, 0));
  }

void DoClose(const string id, string &keys[], string &vals[])
  {
   ulong ticket = (ulong)StringToInteger(CmdGet(keys, vals, "ticket"));
   int deviation = (int)StringToInteger(CmdGet(keys, vals, "deviation"));
   if(deviation <= 0)
      deviation = 20;
   if(!PositionSelectByTicket(ticket))
     {
      WriteResult(id, ResultJson(id, false, 0, "Position " + IntegerToString((long)ticket) + " is not open (already closed?).", ticket, 0));
      return;
     }
   string symbol = PositionGetString(POSITION_SYMBOL);
   g_trade.SetDeviationInPoints(deviation);
   g_trade.SetTypeFillingBySymbol(symbol);
   ResetLastError();
   bool sent = g_trade.PositionClose(ticket, deviation);
   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   bool ok = (sent && RetcodeOk(rc));
   Print("TradeMateBridge CLOSE #", ticket, " -> retcode ", rc, " ", desc);
   WriteResult(id, ResultJson(id, ok, rc, desc, ticket, g_trade.ResultPrice()));
  }

void DoOpenPending(const string id, string &keys[], string &vals[])
  {
   string symbol = CmdGet(keys, vals, "symbol");
   int    otype  = PendingTypeFromName(CmdGet(keys, vals, "order_type"));
   double volume = StringToDouble(CmdGet(keys, vals, "volume"));
   double price  = StringToDouble(CmdGet(keys, vals, "price"));
   double sl     = StringToDouble(CmdGet(keys, vals, "sl"));
   double tp     = StringToDouble(CmdGet(keys, vals, "tp"));
   long   expiry = StringToInteger(CmdGet(keys, vals, "expiration"));
   long   magic  = StringToInteger(CmdGet(keys, vals, "magic"));
   if(otype < 0)
     {
      WriteResult(id, ResultJson(id, false, 0, "Unsupported pending order type '" + CmdGet(keys, vals, "order_type") + "'.", 0, 0));
      return;
     }
   if(!SymbolSelect(symbol, true))
     {
      WriteResult(id, ResultJson(id, false, 0, "Symbol '" + symbol + "' is not available on this broker.", 0, 0));
      return;
     }
   g_trade.SetExpertMagicNumber((ulong)magic);
   g_trade.SetTypeFillingBySymbol(symbol);
   ENUM_ORDER_TYPE_TIME type_time = (expiry > 0 ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC);
   if(expiry > 0)
      expiry += ServerOffsetSeconds();        // UTC epoch from TradeMate -> broker server time
   ResetLastError();
   bool sent = g_trade.OrderOpen(symbol, (ENUM_ORDER_TYPE)otype, volume, 0.0, Norm(symbol, price), Norm(symbol, sl), Norm(symbol, tp),
                                 type_time, (datetime)expiry, "TradeMate");
   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   bool ok = (sent && RetcodeOk(rc));
   ulong order_ticket = (ok ? g_trade.ResultOrder() : 0);
   Print("TradeMateBridge PENDING ", CmdGet(keys, vals, "order_type"), " ", volume, " ", symbol, " @ ", price, " -> retcode ", rc, " ", desc, " order ", order_ticket);
   WriteResult(id, ResultJson(id, ok, rc, desc, order_ticket, price));
  }

void DoCancelOrder(const string id, string &keys[], string &vals[])
  {
   ulong ticket = (ulong)StringToInteger(CmdGet(keys, vals, "ticket"));
   if(!OrderSelect(ticket))
     {
      WriteResult(id, ResultJson(id, false, 0, "Pending order " + IntegerToString((long)ticket) + " does not exist (already filled, cancelled or expired?).", ticket, 0));
      return;
     }
   ResetLastError();
   bool sent = g_trade.OrderDelete(ticket);
   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   bool ok = (sent && RetcodeOk(rc));
   Print("TradeMateBridge CANCEL ORDER #", ticket, " -> retcode ", rc, " ", desc);
   WriteResult(id, ResultJson(id, ok, rc, desc, ticket, 0));
  }

void DoModifyOrder(const string id, string &keys[], string &vals[])
  {
   ulong  ticket = (ulong)StringToInteger(CmdGet(keys, vals, "ticket"));
   double price  = StringToDouble(CmdGet(keys, vals, "price"));
   double sl     = StringToDouble(CmdGet(keys, vals, "sl"));
   double tp     = StringToDouble(CmdGet(keys, vals, "tp"));
   long   expiry = StringToInteger(CmdGet(keys, vals, "expiration"));
   if(!OrderSelect(ticket))
     {
      WriteResult(id, ResultJson(id, false, 0, "Pending order " + IntegerToString((long)ticket) + " does not exist (already filled, cancelled or expired?).", ticket, 0));
      return;
     }
   string symbol = OrderGetString(ORDER_SYMBOL);
   ENUM_ORDER_TYPE_TIME type_time = (expiry > 0 ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC);
   if(expiry > 0)
      expiry += ServerOffsetSeconds();        // UTC epoch from TradeMate -> broker server time
   ResetLastError();
   bool sent = g_trade.OrderModify(ticket, Norm(symbol, price), Norm(symbol, sl), Norm(symbol, tp), type_time, (datetime)expiry, 0.0);
   uint rc = g_trade.ResultRetcode();
   string desc = g_trade.ResultRetcodeDescription();
   bool ok = (sent && RetcodeOk(rc)) || rc == TRADE_RETCODE_NO_CHANGES;
   Print("TradeMateBridge MODIFY ORDER #", ticket, " price ", price, " sl ", sl, " tp ", tp, " -> retcode ", rc, " ", desc);
   WriteResult(id, ResultJson(id, ok, rc, desc, ticket, 0));
  }

// Why a deal happened, as a short word the TradeMate UI understands.
string ReasonName(const long reason)
  {
   if(reason == DEAL_REASON_CLIENT) return("client");
   if(reason == DEAL_REASON_MOBILE) return("mobile");
   if(reason == DEAL_REASON_WEB)    return("web");
   if(reason == DEAL_REASON_EXPERT) return("expert");
   if(reason == DEAL_REASON_SL)     return("sl");
   if(reason == DEAL_REASON_TP)     return("tp");
   if(reason == DEAL_REASON_SO)     return("so");
   return("other");
  }

// history (READ-ONLY): closed positions from the broker's deal history, newest first.
// One entry per position: opening deal(s) + closing deal(s) are merged, partial closes are summed.
void DoHistory(const string id, string &keys[], string &vals[])
  {
   int days  = (int)StringToInteger(CmdGet(keys, vals, "days"));
   int limit = (int)StringToInteger(CmdGet(keys, vals, "limit"));
   if(days < 1)
      days = 30;
   if(days > 365)
      days = 365;
   if(limit < 1)
      limit = 200;
   if(limit > MAX_HISTORY)
      limit = MAX_HISTORY;

   datetime now  = TimeCurrent();
   datetime from = now - (datetime)(days * 86400);
   if(!HistorySelect(from, now + 86400))
     {
      WriteResult(id, ResultJson(id, false, 0, "MT5 could not read the trade history (it may still be loading).", 0, 0));
      return;
     }

   // 1) which positions were closed in this window (newest first, still-open ones skipped)
   ulong pids[];
   int total = HistoryDealsTotal();
   for(int i = total - 1; i >= 0 && ArraySize(pids) < limit; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0)
         continue;
      long type = HistoryDealGetInteger(deal, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;                               // balance / credit / etc.
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT)
         continue;
      ulong pid = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      if(pid == 0 || PositionSelectByTicket(pid))
         continue;                               // unknown id, or only partly closed so far
      bool seen = false;
      for(int k = 0; k < ArraySize(pids); k++)
         if(pids[k] == pid)
           {
            seen = true;
            break;
           }
      if(seen)
         continue;
      int n = ArraySize(pids);
      ArrayResize(pids, n + 1);
      pids[n] = pid;
     }

   // 2) assemble each position from all of its deals
   string trades = "\"trades\":[";
   int written = 0;
   for(int p = 0; p < ArraySize(pids); p++)
     {
      if(!HistorySelectByPosition(pids[p]))
         continue;
      string symbol = "";
      long   dir_in = -1, dir_out = -1, reason = -1;
      double vol_in = 0.0, vol_out = 0.0, px_in = 0.0, px_out = 0.0;
      double profit = 0.0, swap = 0.0, comm = 0.0;
      double sl_in = 0.0, tp_in = 0.0, sl_out = 0.0, tp_out = 0.0;
      datetime t_open = 0, t_close = 0;
      int deals = HistoryDealsTotal();
      for(int j = 0; j < deals; j++)
        {
         ulong d = HistoryDealGetTicket(j);
         if(d == 0)
            continue;
         long type = HistoryDealGetInteger(d, DEAL_TYPE);
         if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
            continue;
         long   entry = HistoryDealGetInteger(d, DEAL_ENTRY);
         double v     = HistoryDealGetDouble(d, DEAL_VOLUME);
         double px    = HistoryDealGetDouble(d, DEAL_PRICE);
         datetime t   = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         profit += HistoryDealGetDouble(d, DEAL_PROFIT);
         swap   += HistoryDealGetDouble(d, DEAL_SWAP);
         comm   += HistoryDealGetDouble(d, DEAL_COMMISSION) + HistoryDealGetDouble(d, DEAL_FEE);
         if(symbol == "")
            symbol = HistoryDealGetString(d, DEAL_SYMBOL);
         if(entry == DEAL_ENTRY_IN)
           {
            if(dir_in < 0)
               dir_in = type;
            if(t_open == 0 || t < t_open)
               t_open = t;
            vol_in += v;
            px_in  += px * v;
            if(sl_in == 0.0)
               sl_in = HistoryDealGetDouble(d, DEAL_SL);
            if(tp_in == 0.0)
               tp_in = HistoryDealGetDouble(d, DEAL_TP);
           }
         else
           {
            dir_out = type;
            vol_out += v;
            px_out  += px * v;
            if(t >= t_close)
              {
               t_close = t;
               reason  = HistoryDealGetInteger(d, DEAL_REASON);
               sl_out  = HistoryDealGetDouble(d, DEAL_SL);
               tp_out  = HistoryDealGetDouble(d, DEAL_TP);
              }
           }
        }
      if(symbol == "" || vol_out <= 0.0)
         continue;
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      // direction of the POSITION: the opening deal's type; if that deal is out of range, the opposite of the closing one
      string direction = "";
      if(dir_in >= 0)
         direction = (dir_in == DEAL_TYPE_BUY ? "buy" : "sell");
      else if(dir_out >= 0)
         direction = (dir_out == DEAL_TYPE_SELL ? "buy" : "sell");
      double sl = (sl_out != 0.0 ? sl_out : sl_in);
      double tp = (tp_out != 0.0 ? tp_out : tp_in);

      if(written > 0)
         trades += ",";
      trades += "{\"ticket\":" + IntegerToString((long)pids[p]);
      trades += ",\"symbol\":" + JsonStr(symbol);
      trades += ",\"type\":" + JsonStr(direction);
      trades += ",\"volume\":" + JsonNum(vol_out, 4);
      trades += ",\"price_open\":" + (vol_in > 0.0 ? JsonNum(px_in / vol_in, digits) : "null");
      trades += ",\"price_close\":" + JsonNum(px_out / vol_out, digits);
      trades += ",\"sl\":" + JsonNumOrNull(sl, digits);
      trades += ",\"tp\":" + JsonNumOrNull(tp, digits);
      trades += ",\"profit\":" + JsonNum(profit, 2);
      trades += ",\"swap\":" + JsonNum(swap, 2);
      trades += ",\"commission\":" + JsonNum(comm, 2);
      trades += ",\"time_open\":" + (t_open > 0 ? IntegerToString((long)t_open) : "null");
      trades += ",\"time_close\":" + IntegerToString((long)t_close);
      trades += ",\"reason\":" + JsonStr(ReasonName(reason)) + "}";
      written++;
     }
   trades += "]";
   WriteResult(id, ResultJson(id, true, 0, "ok", 0, 0, trades));
  }

void ExecuteCommand(const string filename)
  {
   int h = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ, 0, CP_UTF8);
   if(h == INVALID_HANDLE)
      return;                                   // maybe still being written; try next tick
   string keys[], vals[];
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      int n = ArraySize(keys);
      ArrayResize(keys, n + 1);
      ArrayResize(vals, n + 1);
      keys[n] = StringSubstr(line, 0, eq);
      vals[n] = StringSubstr(line, eq + 1);
      StringTrimRight(vals[n]);
      StringTrimLeft(vals[n]);
     }
   FileClose(h);
   FileDelete(filename, FILE_COMMON);           // consume exactly once

   string id  = CmdGet(keys, vals, "id");
   string cmd = CmdGet(keys, vals, "cmd");
   if(id == "")
      return;

   long expires = StringToInteger(CmdGet(keys, vals, "expires"));
   if(expires > 0 && (long)TimeGMT() > expires && cmd != "candles" && cmd != "symbol_info" && cmd != "symbols_search" && cmd != "history")
     {
      WriteResult(id, ResultJson(id, false, 0, "Command expired before MT5 could run it. Nothing was sent to the broker.", 0, 0));
      return;
     }

   // ---- read-only commands (cannot change the account) ----
   if(cmd == "candles")
     {
      DoCandles(id, keys, vals);
      return;
     }
   if(cmd == "symbol_info")
     {
      DoSymbolInfo(id, keys, vals);
      return;
     }
   if(cmd == "symbols_search")
     {
      DoSymbolsSearch(id, keys, vals);
      return;
     }
   if(cmd == "history")
     {
      DoHistory(id, keys, vals);
      return;
     }

   // ---- everything below can change the account ----
   if(!AllowTrading)
     {
      WriteResult(id, ResultJson(id, false, 0, "Trading is switched off in the TradeMateBridge inputs (AllowTrading=false).", 0, 0));
      return;
     }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
     {
      WriteResult(id, ResultJson(id, false, 0, "REFUSED: this is not a DEMO account.", 0, 0));
      return;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      WriteResult(id, ResultJson(id, false, 10027, "Algo Trading is switched off in MT5. Enable the 'Algo Trading' toolbar button and tick 'Allow Algo Trading' in the EA properties.", 0, 0));
      return;
     }

   if(cmd == "open")
      DoOpen(id, keys, vals);
   else if(cmd == "modify")
      DoModify(id, keys, vals);
   else if(cmd == "close")
      DoClose(id, keys, vals);
   else if(cmd == "open_pending")
      DoOpenPending(id, keys, vals);
   else if(cmd == "cancel_order")
      DoCancelOrder(id, keys, vals);
   else if(cmd == "modify_order")
      DoModifyOrder(id, keys, vals);
   else
      WriteResult(id, ResultJson(id, false, 0, "Unknown command '" + cmd + "'.", 0, 0));

   WriteSnapshot();                             // publish the new broker state immediately
  }

void ProcessCommands()
  {
   string name;
   string pending[];
   long handle = FileFindFirst("trademate_cmd_*.txt", name, FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return;
   do
     {
      int n = ArraySize(pending);
      ArrayResize(pending, n + 1);
      pending[n] = name;
     }
   while(FileFindNext(handle, name));
   FileFindClose(handle);
   for(int i = 0; i < ArraySize(pending); i++)
      ExecuteCommand(pending[i]);
  }
//+------------------------------------------------------------------+
