import { Suspense } from "react";
import { InspectionHistory } from "@/components/InspectionHistory";

export const metadata = {
  title: "Inspection History | TradePro Educational Analytics",
  description: "View persistent educational inspection runs and historical Boolean rule replay audit logs.",
};

export default function InspectionHistoryPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-slate-500">Loading inspection history...</div>}>
      <InspectionHistory />
    </Suspense>
  );
}
