from types import SimpleNamespace

from run_trader import _bind_exit_connection, _bind_quote_connection


def test_selected_protective_connection_owns_observation_and_order_transmission():
    original = object()
    selected = SimpleNamespace(client_id=189, market_data_type=1)
    manager = SimpleNamespace(
        ib_conn=original,
        order_manager=SimpleNamespace(ib_conn=original),
    )

    _bind_exit_connection(manager, selected)

    assert manager.ib_conn is selected
    assert manager.order_manager.ib_conn is selected
    assert manager.order_manager.ib_conn.client_id == 189
    assert manager.order_manager.ib_conn.market_data_type == 1


def test_quote_connection_is_read_only_and_never_rebinds_order_manager():
    order_conn = SimpleNamespace(client_id=189)
    quote_conn = SimpleNamespace(client_id=118)
    manager = SimpleNamespace(
        ib_conn=order_conn, quote_ib_conn=None,
        order_manager=SimpleNamespace(ib_conn=order_conn))

    _bind_quote_connection(manager, quote_conn)

    assert manager.quote_ib_conn is quote_conn
    assert manager.ib_conn is order_conn
    assert manager.order_manager.ib_conn is order_conn
