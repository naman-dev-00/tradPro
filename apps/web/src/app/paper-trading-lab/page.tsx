import { PaperTradingLab } from "@/components/PaperTradingLab";

export const metadata = {
  title: "Paper Trading Runtime & OMS | TradePro",
  description: "Deterministic paper execution engine with pre-trade risk controls, double-entry cash ledger, and emergency kill switches. Simulated environment — no live orders.",
};

export default function PaperTradingLabPage() {
  return <PaperTradingLab />;
}
