"""Public API contract; production-derived narrative omitted."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

import fidelity_curate as FC


HDR = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Exchange Quantity,"
       "Exchange Currency,Currency,Price,Quantity,Exchange Rate,Commission,Fees,"
       "Accrued Interest,Amount,Settlement Date")
FOOTER = ('\n"Brokerage services provided by Fidelity Brokerage Services LLC."\n\n"1038360.4.0"\n'
          "\nDate downloaded 08/21/2037 11:38 pm\n")


def row(date, action, qty, price, amount, symbol="", acct="100000001", name="EXAMPLE ACCOUNT"):
    return ('%s,%s,%s,"%s","%s",desc,Margin,0,"",USD,%s,%s,0,0,0,"","%s",%s'
            % (date, name, acct, action, symbol, price, qty, amount, date))


def export(tmp_path, *rows, name="Accounts_History.csv", mtime=None):
    p = tmp_path / name
    p.write_text("﻿\n\n" + HDR + "\n" + "\n".join(rows) + FOOTER)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def curate(tmp_path, as_of="2037-08-22", until=None):
    return FC.curate(str(tmp_path / "Accounts_History*.csv"),
                     as_of=dt.date.fromisoformat(as_of),
                     until=dt.date.fromisoformat(until) if until else None)


BUY_OPEN = "YOU BOUGHT OPENING TRANSACTION CALL (TESTA) APPLE INC AUG 28 37 $320 (100 SHS)"
SELL_CLOSE = "YOU SOLD CLOSING TRANSACTION CALL (TESTA) APPLE INC AUG 28 37 $320 (100 SHS)"
SELL_OPEN = "YOU SOLD OPENING TRANSACTION PUT (SYMI) EXAMPLE COMPANY COM AUG 28 37 $82 (100 SHS)"
BUY_CLOSE = "YOU BOUGHT CLOSING TRANSACTION PUT (SYMI) EXAMPLE COMPANY COM AUG 28 37 $82 (100 SHS)"
