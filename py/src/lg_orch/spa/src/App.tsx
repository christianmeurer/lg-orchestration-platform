import { useState, useEffect } from 'react';
import { Activity } from 'lucide-react';

export default function App() {
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const status = 'idle';

  useEffect(() => {
    fetch('/v1/runs')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data && data.runs) setRuns(data.runs);
      })
      .catch(console.error);
  }, []);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: '#0d1117', color: '#c9d1d9', fontFamily: 'sans-serif' }}>
      <header style={{ padding: '12px 16px', background: '#161b22', borderBottom: '1px solid #30363d', display: 'flex', alignItems: 'center', gap: '12px' }}>
        <h1 style={{ fontSize: '15px', margin: 0 }}>LG <span style={{ color: '#58a6ff' }}>Orchestration</span> (React)</h1>
        <span style={{ background: '#21262d', padding: '2px 8px', borderRadius: '12px', fontSize: '11px' }}>{status}</span>
      </header>
      
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <aside style={{ width: '240px', background: '#161b22', borderRight: '1px solid #30363d', overflowY: 'auto' }}>
          <div style={{ padding: '8px 12px', fontSize: '11px', textTransform: 'uppercase', color: '#8b949e', borderBottom: '1px solid #30363d' }}>
            Run History
          </div>
          {runs.map((run: any) => (
            <div 
              key={run.run_id} 
              onClick={() => setSelectedRunId(run.run_id)}
              style={{ 
                padding: '8px 12px', 
                cursor: 'pointer', 
                borderLeft: `3px solid ${selectedRunId === run.run_id ? '#58a6ff' : 'transparent'}`,
                background: selectedRunId === run.run_id ? '#21262d' : 'transparent'
              }}>
              <div style={{ fontSize: '11px', fontFamily: 'monospace' }}>{run.run_id.slice(0,8)}...</div>
              <div style={{ fontSize: '10px', color: '#8b949e', marginTop: '4px' }}>
                <span style={{ display: 'inline-block', padding: '2px 6px', background: '#1a3a5c', color: '#58a6ff', borderRadius: '8px', marginRight: '6px' }}>{run.status}</span>
                {new Date(run.created_at).toLocaleTimeString()}
              </div>
            </div>
          ))}
        </aside>
        
        <main style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {selectedRunId ? (
            <div style={{ textAlign: 'center' }}>
              <Activity size={48} color="#58a6ff" style={{ marginBottom: '16px' }} />
              <h2>Run: {selectedRunId}</h2>
              <p style={{ color: '#8b949e' }}>Event streaming requires SSE implementation in React.</p>
            </div>
          ) : (
            <div style={{ color: '#8b949e' }}>Select a run from the sidebar to view details.</div>
          )}
        </main>
      </div>
    </div>
  );
}
