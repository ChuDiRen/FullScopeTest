# 大熊AI测试平台 JavaScript SDK

## Installation

```bash
npm install @fullscopetest/sdk
```

## Usage

```typescript
import { 大熊AI测试平台Client } from '@fullscopetest/sdk';

const client = new 大熊AI测试平台Client({
  baseUrl: 'https://api.fullscopetest.com',
  apiToken: 'fst_xxx',
});

// Run tests
const result = await client.runTests({ projectId: 1, testType: 'api' });
console.log(`Pass rate: ${result.passed}/${result.total}`);
```
