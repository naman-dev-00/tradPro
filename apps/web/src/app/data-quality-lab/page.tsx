import { DataQualityLab } from "@/components/DataQualityLab";

export const metadata = {
  title: "Dataset Quality & Provenance Lab | TradePro",
  description: "Deterministic schema validation, timestamp continuity, OHLCV bounds, and checksum verification for packaged synthetic datasets.",
};

export default function DataQualityLabPage() {
  return <DataQualityLab />;
}
