import { Metadata } from "next";
import { MultiSeriesLabWorkspace } from "@/components/MultiSeriesLab";

export const metadata: Metadata = {
  title: "Multi-Series Rule Inspection Lab - TradePro",
  description:
    "Inspect Boolean strategy rules independently across multiple packaged synthetic subject datasets. Educational synthetic data only.",
};

export default function MultiSeriesLabPage() {
  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <MultiSeriesLabWorkspace />
    </main>
  );
}
