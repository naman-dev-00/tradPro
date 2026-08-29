import { HistoricalReplayLab } from "@/components/HistoricalReplayLab";

export const metadata = {
  title: "Historical Replay Lab | TradePro Educational Analytics",
  description:
    "Evaluate Boolean strategy rules repeatedly across historical synthetic candle timestamps with zero financial simulation or recommendations.",
};

export default function HistoricalReplayLabPage() {
  return <HistoricalReplayLab />;
}
