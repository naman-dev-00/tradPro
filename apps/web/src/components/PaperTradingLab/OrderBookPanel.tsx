"use client";

import React, { useState } from "react";
import { Order, Fill } from "../../lib/api";

interface Props {
  orders: Order[];
  fills: Fill[];
  onCancelOrder: (orderId: string) => void;
  onInspectOrder: (order: Order) => void;
}

export const OrderBookPanel: React.FC<Props> = ({
  orders,
  fills,
  onCancelOrder,
  onInspectOrder,
}) => {
  const [tab, setTab] = useState<"ORDERS" | "FILLS">("ORDERS");

  const openOrders = orders.filter((o) => o.status === "ACCEPTED" || o.status === "PARTIALLY_FILLED");

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
      {/* Tabs */}
      <div className="flex items-center justify-between border-b border-slate-800 pb-3 mb-3">
        <div className="flex gap-2">
          <button
            onClick={() => setTab("ORDERS")}
            className={`px-3 py-1 text-xs font-semibold rounded transition-colors ${
              tab === "ORDERS" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Orders ({orders.length})
          </button>
          <button
            onClick={() => setTab("FILLS")}
            className={`px-3 py-1 text-xs font-semibold rounded transition-colors ${
              tab === "FILLS" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Fills ({fills.length})
          </button>
        </div>

        {tab === "ORDERS" && openOrders.length > 0 && (
          <span className="text-xs text-emerald-400 font-medium">
            {openOrders.length} active open
          </span>
        )}
      </div>

      {/* Orders Tab */}
      {tab === "ORDERS" && (
        <div className="overflow-x-auto">
          {orders.length === 0 ? (
            <div className="text-center py-6 text-xs text-slate-400">No orders placed yet.</div>
          ) : (
            <table className="w-full text-left text-xs text-slate-300">
              <thead className="bg-slate-800/60 text-slate-400 uppercase tracking-wider text-[10px]">
                <tr>
                  <th className="py-2 px-3">Seq #</th>
                  <th className="py-2 px-3">Side</th>
                  <th className="py-2 px-3">Type</th>
                  <th className="py-2 px-3">Qty</th>
                  <th className="py-2 px-3">Filled</th>
                  <th className="py-2 px-3">Limit</th>
                  <th className="py-2 px-3">Status</th>
                  <th className="py-2 px-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {orders.map((o) => {
                  const isOpen = o.status === "ACCEPTED" || o.status === "PARTIALLY_FILLED";
                  return (
                    <tr key={o.id} className="hover:bg-slate-800/40 transition-colors">
                      <td className="py-2.5 px-3 font-mono">#{o.order_sequence_number}</td>
                      <td className={`py-2.5 px-3 font-bold ${o.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>
                        {o.side}
                      </td>
                      <td className="py-2.5 px-3">{o.order_type}</td>
                      <td className="py-2.5 px-3">{o.quantity}</td>
                      <td className="py-2.5 px-3">{o.filled_quantity}</td>
                      <td className="py-2.5 px-3">{o.limit_price ? `₹${o.limit_price}` : "—"}</td>
                      <td className="py-2.5 px-3">
                        <span
                          className={`px-2 py-0.5 text-[10px] font-semibold rounded-full ${
                            o.status === "FILLED"
                              ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                              : o.status === "ACCEPTED"
                              ? "bg-blue-950 text-blue-300 border border-blue-800"
                              : o.status === "PARTIALLY_FILLED"
                              ? "bg-cyan-950 text-cyan-300 border border-cyan-800"
                              : o.status === "RISK_REJECTED"
                              ? "bg-rose-950 text-rose-300 border border-rose-800"
                              : "bg-slate-800 text-slate-400 border border-slate-700"
                          }`}
                        >
                          {o.status}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right space-x-2">
                        <button
                          onClick={() => onInspectOrder(o)}
                          className="px-2 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-[11px]"
                        >
                          Audit
                        </button>
                        {isOpen && (
                          <button
                            onClick={() => onCancelOrder(o.id)}
                            className="px-2 py-0.5 bg-rose-900/50 hover:bg-rose-800 text-rose-300 border border-rose-700 rounded text-[11px]"
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Fills Tab */}
      {tab === "FILLS" && (
        <div className="overflow-x-auto">
          {fills.length === 0 ? (
            <div className="text-center py-6 text-xs text-slate-400">No fills recorded yet.</div>
          ) : (
            <table className="w-full text-left text-xs text-slate-300">
              <thead className="bg-slate-800/60 text-slate-400 uppercase tracking-wider text-[10px]">
                <tr>
                  <th className="py-2 px-3">Fill ID</th>
                  <th className="py-2 px-3">Side</th>
                  <th className="py-2 px-3">Qty</th>
                  <th className="py-2 px-3">Price</th>
                  <th className="py-2 px-3">Fee</th>
                  <th className="py-2 px-3">Candle Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {fills.map((f) => (
                  <tr key={f.id} className="hover:bg-slate-800/40 transition-colors">
                    <td className="py-2.5 px-3 font-mono text-slate-400">{f.id.slice(0, 8)}...</td>
                    <td className={`py-2.5 px-3 font-bold ${f.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>
                      {f.side}
                    </td>
                    <td className="py-2.5 px-3 font-semibold">{f.quantity}</td>
                    <td className="py-2.5 px-3">₹{f.price}</td>
                    <td className="py-2.5 px-3 text-slate-400">₹{f.fee}</td>
                    <td className="py-2.5 px-3 text-slate-400">
                      {new Date(f.candle_timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
};
