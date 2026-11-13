import type { Env } from './types';

export function handleDashboardRequest(
  request: Request,
  env: Env,
  hashDashboardToken: (token: string) => Promise<string>
): Promise<Response | null>;
