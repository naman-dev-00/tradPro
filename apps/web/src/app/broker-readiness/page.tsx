import React, { Suspense } from "react";
import { BrokerReadiness } from "@/components/BrokerReadiness";

export const metadata = {
  title: "Broker Readiness & Live Connection | TradePro",
  description: "Secure broker connection management, live readiness gate evaluation, and read-only portfolio snapshots.",
};

export default function BrokerReadinessPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-slate-950 text-slate-400 p-8">Loading Broker Readiness...</div>}>
      <BrokerReadiness />
    </Suspense>
  );
}
