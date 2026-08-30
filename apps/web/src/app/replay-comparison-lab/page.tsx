import { Suspense } from "react";
import { ReplayComparisonLab } from "@/components/ReplayComparisonLab";

export const metadata = {
  title: "Replay Comparison Lab - TradePro",
  description: "Deterministic historical replay comparison, reproducibility verification, and educational data exports.",
};

export default function ReplayComparisonLabPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-slate-400">Loading Replay Comparison Lab...</div>}>
      <ReplayComparisonLab />
    </Suspense>
  );
}
