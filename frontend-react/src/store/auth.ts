import { create } from 'zustand';

export interface User {
  email: string;
  tenant: string;
  tenant_name?: string;
  role: string;
  plan?: string;
}

interface AuthState {
  user: User | null;
  ready: boolean; // initial /me check complete
  setUser: (u: User | null) => void;
  setReady: (r: boolean) => void;
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,
  setUser: (user) => set({ user }),
  setReady: (ready) => set({ ready }),
}));
