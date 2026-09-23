// The evaluator's fixed Playwright config (containers/evaluator.Dockerfile).
//
// The frozen checks are spec files only. Where they run, what they report and
// where evidence goes are decided here, not by the checks: the controller reads
// /out/report.json, and nothing else in /out is trusted.
import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: '/checks',
  outputDir: '/out/test-results',
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['json', { outputFile: '/out/report.json' }], ['line']],
  use: {
    // The demo, by the sandbox's name on the egress network (evaluation.py).
    baseURL: process.env.APP_URL,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
