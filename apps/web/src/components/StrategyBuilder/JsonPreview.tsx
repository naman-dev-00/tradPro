import React, { useState } from "react";

interface JsonPreviewProps {
  strategyJson: any;
}

export function JsonPreview({ strategyJson }: JsonPreviewProps) {
  const [copied, setCopied] = useState(false);
  const jsonStr = JSON.stringify(strategyJson, null, 2);

  const handleCopy = () => {
    navigator.clipboard.writeText(jsonStr);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 text-slate-100 font-sans h-full flex flex-col">
      <div className="border-b border-slate-800 pb-2 mb-3 flex justify-between items-center">
        <h3 className="font-bold text-sm text-indigo-400 tracking-wide uppercase">JSON Definition</h3>
        <button
          onClick={handleCopy}
          className="bg-indigo-600 hover:bg-indigo-500 text-slate-100 text-xs px-2.5 py-1 rounded font-medium transition"
        >
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>

      <pre className="flex-1 overflow-auto text-[11px] font-mono text-slate-300 bg-slate-900/60 p-3 rounded border border-slate-900 select-all leading-relaxed whitespace-pre max-h-[160px]">
        {jsonStr}
      </pre>
    </div>
  );
}
