import type { Direction } from './types';

export interface AnalysisCandle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type AnalysisTimeframe = '1m' | '5m' | '15m' | '1h' | '4h';

export interface IctOpportunity {
  direction: Direction;
  entryPrice: number;
  stopLoss: number;
  takeProfit: number;
  timestamp: number;
  auditTrail: string[];
  setup15mTimestamp: number;
  confirmation5mTimestamp: number;
  refinement1mTimestamp: number;
}

export interface IctAnalysisResult {
  opportunities: IctOpportunity[];
}

export class IctAnalysisError extends Error {}

export function analyzeIctCandles(
  timeframes: Record<'4h' | '1h' | '15m' | '5m' | '1m', AnalysisCandle[]>
): IctAnalysisResult;

export function fetchIctAnalysis(symbol: string): Promise<IctAnalysisResult>;

export function fetchHistoricalCandles(
  symbol: string,
  interval: AnalysisTimeframe
): Promise<AnalysisCandle[]>;

export function formatIctOpportunity(symbol: string, opportunity: IctOpportunity): string;
