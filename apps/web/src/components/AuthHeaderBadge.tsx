"use client";

import React, { useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";

export function AuthHeaderBadge() {
  const { user, loading, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    try {
      setLoggingOut(true);
      await logout();
      router.refresh();
    } catch (err) {
      console.error("Logout failed:", err);
    } finally {
      setLoggingOut(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-slate-400 animate-pulse" aria-label="Loading user state">
        <div className="h-6 w-16 bg-slate-800 rounded"></div>
      </div>
    );
  }

  if (!user) {
    const returnParam = pathname ? `?returnUrl=${encodeURIComponent(pathname)}` : "";
    return (
      <a
        href={`/login${returnParam}`}
        aria-label="Sign in to your account"
        className="inline-flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/20 focus:outline-none focus:ring-2 focus:ring-emerald-400 transition"
      >
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1" />
        </svg>
        Sign In
      </a>
    );
  }

  const roleStyles = {
    ADMIN: "bg-purple-500/20 text-purple-300 border-purple-500/30",
    EDITOR: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
    VIEWER: "bg-slate-700/50 text-slate-300 border-slate-600",
  }[user.role] || "bg-slate-700 text-slate-300 border-slate-600";

  return (
    <div className="flex items-center gap-2 sm:gap-3 bg-slate-900/80 border border-slate-800 px-2 sm:px-2.5 py-1 rounded-lg shrink-0" aria-label="User profile and session badge">
      <div className="flex items-center gap-1.5 min-w-0">
        <div className="w-2 h-2 rounded-full bg-emerald-400 shrink-0" aria-hidden="true"></div>
        <span className="text-xs font-medium text-slate-200 truncate max-w-[70px] xs:max-w-[100px] sm:max-w-none" title={user.username}>
          {user.username}
        </span>
        <span className={`text-[10px] font-mono uppercase px-1.5 py-0.5 rounded border font-semibold shrink-0 ${roleStyles}`}>
          {user.role}
        </span>
      </div>

      <button
        onClick={handleLogout}
        disabled={loggingOut}
        title="Sign Out"
        aria-label="Sign out of your account"
        className="text-xs text-slate-400 hover:text-rose-400 focus:outline-none focus:ring-1 focus:ring-rose-400 rounded px-1 transition font-medium cursor-pointer shrink-0"
      >
        {loggingOut ? "..." : "Sign Out"}
      </button>
    </div>
  );
}
