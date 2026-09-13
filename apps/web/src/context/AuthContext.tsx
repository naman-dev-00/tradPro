"use client";

import React, { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { User, getCurrentUser, loginUser as apiLoginUser, logoutUser as apiLogoutUser } from "@/lib/api";

interface AuthContextType {
  user: User | null;
  loading: boolean;
  login: (usernameOrEmail: string, password: string) => Promise<User>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const pathname = usePathname();
  const router = useRouter();

  const refreshUser = async () => {
    try {
      const u = await getCurrentUser();
      setUser(u);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refreshUser();
  }, []);

  useEffect(() => {
    if (!loading && !user && pathname) {
      const protectedRoutes = [
        "/builder",
        "/indicator-lab",
        "/rule-lab",
        "/multi-series-lab",
        "/historical-replay-lab",
        "/inspection-history",
        "/replay-comparison-lab",
        "/data-quality-lab",
      ];
      const isProtected = protectedRoutes.some((r) => pathname === r || pathname.startsWith(`${r}/`));
      if (isProtected) {
        router.replace(`/login?returnUrl=${encodeURIComponent(pathname)}`);
      }
    }
  }, [loading, user, pathname, router]);

  const login = async (usernameOrEmail: string, password: string): Promise<User> => {
    const loggedInUser = await apiLoginUser(usernameOrEmail, password);
    setUser(loggedInUser);
    return loggedInUser;
  };

  const logout = async () => {
    await apiLogoutUser();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
