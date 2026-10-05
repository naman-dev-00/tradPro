import { MarketDataLab } from "@/components/MarketDataLab";

export const metadata = {
  title: "Market Data Lab | TradePro",
  description: "Read-only inspection of Upstox V3 historical and current-day completed candles with cryptographic provenance.",
};

export default function MarketDataLabPage() {
  return <MarketDataLab />;
}
