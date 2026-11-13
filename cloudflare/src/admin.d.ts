import type { Env } from './types';

export function handleAdminRequest(
  request: Request,
  env: Env,
  hashDashboardToken: (token: string) => Promise<string>
): Promise<Response | null>;
