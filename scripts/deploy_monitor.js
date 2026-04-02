const { execSync } = require('child_process');
const https = require('https');

// Configuration
const POLLING_INTERVAL = 10000; // 10 seconds
const TIMEOUT = 600000; // 10 minutes
const REPO_OWNER = 'sean-park-funda';
const REPO_NAME = 'sonolbot_web';

// Get current commit hash
let commitHash;
try {
  commitHash = execSync('git rev-parse HEAD').toString().trim();
  console.log(`Checking deployment status for commit: ${commitHash.substring(0, 7)}...`);
} catch (e) {
  console.error('Error getting commit hash:', e.message);
  process.exit(1);
}

// Function to fetch status from GitHub
function checkStatus() {
  const options = {
    hostname: 'api.github.com',
    path: `/repos/${REPO_OWNER}/${REPO_NAME}/commits/${commitHash}/status`,
    method: 'GET',
    headers: {
      'User-Agent': 'DeployMonitorScript',
      'Authorization': `token ${process.env.GITHUB_TOKEN || execSync('gh auth token').toString().trim()}`
    }
  };

  const req = https.request(options, (res) => {
    let data = '';
    res.on('data', (chunk) => data += chunk);
    res.on('end', () => {
      if (res.statusCode !== 200) {
        console.error(`GitHub API Error: ${res.statusCode} - ${data}`);
        return;
      }

      const response = JSON.parse(data);
      const state = response.state;
      const statuses = response.statuses || [];
      
      // Look for Vercel context
    // Check standard statuses
    const vercelStatus = statuses.find(s => s.context && s.context.includes('Vercel'));
    
    if (vercelStatus) {
      console.log(`[${new Date().toLocaleTimeString()}] Vercel Status (Commit Status): ${vercelStatus.state}`);
      if (vercelStatus.state === 'success') {
        console.log('\n✅ Deployment Successful!');
        console.log(`Url: ${vercelStatus.target_url}`);
        process.exit(0);
      } else if (vercelStatus.state === 'failure' || vercelStatus.state === 'error') {
        console.error('\n❌ Deployment Failed!');
        process.exit(1);
      }
      return;
    }

    // Fallback: Check Check Runs (for GitHub Actions/Vercel integration)
    const checkRunsPath = `/repos/${REPO_OWNER}/${REPO_NAME}/commits/${commitHash}/check-runs`;
    const checkRunsOptions = {
      hostname: 'api.github.com',
      path: checkRunsPath,
      method: 'GET',
      headers: {
        'User-Agent': 'DeployMonitorScript',
        'Authorization': `token ${process.env.GITHUB_TOKEN || execSync('gh auth token').toString().trim()}`
      }
    };

    const checkRunsReq = https.request(checkRunsOptions, (crRes) => {
      let crData = '';
      crRes.on('data', (chunk) => crData += chunk);
      crRes.on('end', () => {
        if (crRes.statusCode !== 200) {
           console.error(`Check Runs API Error: ${crRes.statusCode}`);
           return;
        }
        
        const crResponse = JSON.parse(crData);
        const checkRuns = crResponse.check_runs || [];
        const vercelRun = checkRuns.find(r => r.name && r.name.includes('Vercel'));

        if (vercelRun) {
           console.log(`[${new Date().toLocaleTimeString()}] Vercel Status (Check Run): ${vercelRun.status} - ${vercelRun.conclusion}`);
           
           if (vercelRun.status === 'completed') {
             if (vercelRun.conclusion === 'success') {
               console.log('\n✅ Deployment Successful!');
               console.log(`Url: ${vercelRun.html_url}`);
               process.exit(0);
             } else {
               console.error(`\n❌ Deployment Failed! (Conclusion: ${vercelRun.conclusion})`);
               process.exit(1);
             }
           }
        } else {
           console.log(`[${new Date().toLocaleTimeString()}] Waiting for Vercel status...`);
        }
      });
    });
    checkRunsReq.on('error', (e) => console.error(e));
    checkRunsReq.end();

    });
  });

  req.on('error', (e) => {
    console.error(`Problem with request: ${e.message}`);
  });

  req.end();
}

// Start polling
console.log('Starting deployment monitor...');
const startTime = Date.now();
const interval = setInterval(() => {
  if (Date.now() - startTime > TIMEOUT) {
    console.error('\nTimeout waiting for deployment.');
    process.exit(1);
  }
  checkStatus();
}, POLLING_INTERVAL);

// Initial check
checkStatus();
