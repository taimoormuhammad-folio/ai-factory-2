import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MobileFrame } from './Phone';
import { mockups, smokeFeatures, smokeReport, sourceFiles, specDocument } from './gates';
import { MobileBrand } from './Phone';
import { productBrandInitial, productFilenameSlug, productShortBrand } from './productBrand.js';

function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadText(filename, text, type = 'text/plain') {
  downloadBlob(filename, new Blob([text], { type }));
}

function crc32(bytes) {
  let crc = ~0;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc >>> 0;
}

function zipBlob(files) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;
  for (const file of files) {
    const name = encoder.encode(file.path);
    const data = encoder.encode(file.code);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, name.length, true);
    const localBytes = new Uint8Array(local.buffer);
    parts.push(localBytes, name, data);
    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true);
    entry.setUint16(4, 20, true);
    entry.setUint16(6, 20, true);
    entry.setUint32(16, crc, true);
    entry.setUint32(20, data.length, true);
    entry.setUint32(24, data.length, true);
    entry.setUint16(28, name.length, true);
    entry.setUint32(42, offset, true);
    central.push(new Uint8Array(entry.buffer), name);
    offset += localBytes.length + name.length + data.length;
  }
  const centralSize = central.reduce((sum, part) => sum + part.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)], { type: 'application/zip' });
}

function GateActions({ onApprove, onReject, needsRiskNote = false, defaultApprover = 'Console User' }) {
  const [rejecting, setRejecting] = useState(false);
  const [comment, setComment] = useState('');
  const [riskNote, setRiskNote] = useState('');
  const [approver, setApprover] = useState(defaultApprover);
  const [error, setError] = useState('');
  return (
    <form
      className="gate-actions"
      onSubmit={(event) => {
        event.preventDefault();
        if (!comment.trim()) {
          setError('Add a comment before rejecting.');
          return;
        }
        onReject(comment.trim(), { approver: approver.trim() || defaultApprover });
      }}
    >
      <label htmlFor="gate-approver">Your name
        <input id="gate-approver" value={approver} onChange={(event) => setApprover(event.target.value)} placeholder="Who is approving?" />
      </label>
      {needsRiskNote && !rejecting && (
        <label htmlFor="gate-risk">Risk note (required for merge)
          <textarea id="gate-risk" value={riskNote} placeholder="In your own words, at least 20 characters" onChange={(event) => { setRiskNote(event.target.value); setError(''); }} />
        </label>
      )}
      {rejecting && (
        <label htmlFor="gate-comment">Comment
          <textarea id="gate-comment" value={comment} placeholder="Tell the team what needs to change" onChange={(event) => { setComment(event.target.value); setError(''); }} autoFocus />
        </label>
      )}
      {error && <p role="alert" className="error">{error}</p>}
      <div className="modal-actions">
        <button
          type="button"
          className="primary"
          onClick={() => {
            if (!approver.trim() || approver.trim().length < 2) {
              setError('Enter your name (who is approving).');
              return;
            }
            if (needsRiskNote && riskNote.trim().length < 20) {
              setError('Merge gate needs a risk note of at least 20 characters.');
              return;
            }
            onApprove({ approver: approver.trim(), risk_note: riskNote.trim() });
          }}
        >
          Approve
        </button>
        {rejecting
          ? <button type="submit" className="danger">Confirm reject</button>
          : <button type="button" className="secondary" onClick={() => setRejecting(true)}>Reject</button>}
      </div>
    </form>
  );
}

function fileTree(files) {
  const root = [];
  for (const file of files) {
    const parts = file.path.split('/');
    let level = root;
    parts.forEach((part, index) => {
      const last = index === parts.length - 1;
      let node = level.find((item) => item.name === part);
      if (!node) {
        node = last ? { name: part, file } : { name: part, children: [] };
        level.push(node);
      }
      if (!last) level = node.children;
    });
  }
  return root;
}

function FolderTree({ nodes, selected, onSelect, depth = 0 }) {
  return nodes.map((node) => (
    node.file
      ? <button type="button" key={node.file.path} className={`tree-file ${selected?.path === node.file.path ? 'selected' : ''}`} style={{ paddingLeft: 12 + depth * 14 }} onClick={() => onSelect(node.file)}>{node.name}</button>
      : <div key={node.name}><div className="tree-dir" style={{ paddingLeft: 12 + depth * 14 }}>{node.name}</div><FolderTree nodes={node.children} selected={selected} onSelect={onSelect} depth={depth + 1} /></div>
  ));
}

function SpecReview({ project, productBrand, documentText: liveText }) {
  const documentText = liveText || specDocument(project, productBrand);
  const slug = productFilenameSlug(productBrand);
  return (
    <div className="gate-body">
      <div className="gate-toolbar"><p>Review the specification before the team continues.</p><button type="button" className="secondary" onClick={() => downloadText(`${slug}-specification.md`, documentText, 'text/markdown')}>Download specification</button></div>
      <article className="spec-sheet">{documentText}</article>
    </div>
  );
}

function ArchitectureReview({ productBrand, documentText, summary }) {
  const slug = productFilenameSlug(productBrand);
  const body = documentText || 'Architecture and design documents are not available yet.';
  return (
    <div className="gate-body">
      <div className="gate-toolbar">
        <p>{summary || 'Review architecture, design system, and backlog before build starts.'}</p>
        <button type="button" className="secondary" onClick={() => downloadText(`${slug}-architecture-pack.md`, body, 'text/markdown')}>Download pack</button>
      </div>
      <article className="spec-sheet">{body}</article>
    </div>
  );
}

function DeployReview({ productBrand, files }) {
  const slug = productFilenameSlug(productBrand);
  const list = files?.length ? files : sourceFiles;
  const tree = useMemo(() => fileTree(list), [list]);
  const [selected, setSelected] = useState(list[0]);
  useEffect(() => { setSelected(list[0]); }, [list]);
  if (!selected) return null;
  return (
    <div className="gate-body">
      <div className="gate-toolbar">
        <p>Open a file in the project structure, then approve the build.</p>
        <div className="gate-toolbar-actions">
          <button type="button" className="secondary" onClick={() => downloadText(selected.path.split('/').pop(), selected.code)}>Download file</button>
          <button type="button" className="secondary" onClick={() => downloadBlob(`${slug}-source.zip`, zipBlob(list))}>Download project</button>
        </div>
      </div>
      <div className="code-review">
        <div className="file-tree" role="tree" aria-label="Project structure"><FolderTree nodes={tree} selected={selected} onSelect={setSelected} /></div>
        <pre className="code-view" aria-label={selected.path}><span className="code-path">{selected.path}</span>{selected.code}</pre>
      </div>
    </div>
  );
}

function SmokeReview({ productBrand, documentText }) {
  const slug = productFilenameSlug(productBrand);
  if (documentText) {
    return (
      <div className="gate-body">
        <div className="gate-toolbar"><p>Release verification pack.</p><button type="button" className="secondary" onClick={() => downloadText(`${slug}-release.md`, documentText, 'text/markdown')}>Download report</button></div>
        <article className="spec-sheet">{documentText}</article>
      </div>
    );
  }
  return (
    <div className="gate-body">
      <div className="gate-toolbar"><p>Smoke results grouped by feature.</p><button type="button" className="secondary" onClick={() => downloadText(`${slug}-smoke-report.md`, smokeReport(productBrand), 'text/markdown')}>Download report</button></div>
      <div className="smoke-list">
        {smokeFeatures.map((group) => (
          <section key={group.feature}>
            <h3>{group.feature}</h3>
            {group.tests.map((test) => <p key={test.name}><b>{test.result}</b><span>{test.name}</span></p>)}
          </section>
        ))}
      </div>
    </div>
  );
}

const titles = {
  spec: 'Review specification',
  architecture: 'Review architecture & design',
  design: 'Review mockups',
  deploy: 'Review build',
  smoke: 'Review smoke tests',
};

export function GateReview({
  kind,
  project,
  productBrand = 'Project',
  displayName,
  documentText,
  summary,
  sourceFiles: liveFiles,
  needsRiskNote = false,
  onClose,
  onApprove,
  onReject,
}) {
  const brand = productShortBrand(displayName || productBrand || project?.projectName || 'Project');
  const ref = useRef();
  useEffect(() => { ref.current.showModal(); }, []);
  return (
    <dialog ref={ref} className="gate-dialog" onCancel={onClose} onClick={(event) => { if (event.target === ref.current) onClose(); }}>
      <div className="modal-heading"><h2>{titles[kind] || 'Review'}</h2><button type="button" onClick={onClose} aria-label="Close dialog">×</button></div>
      {kind === 'spec' && <SpecReview project={project} productBrand={brand} documentText={documentText} />}
      {kind === 'architecture' && <ArchitectureReview productBrand={brand} documentText={documentText} summary={summary} />}
      {kind === 'deploy' && <DeployReview productBrand={brand} files={liveFiles} />}
      {kind === 'smoke' && <SmokeReview productBrand={brand} documentText={documentText} />}
      <GateActions onApprove={onApprove} onReject={onReject} needsRiskNote={needsRiskNote} />
    </dialog>
  );
}

function MockScreen({ id, productBrand = 'Project' }) {
  const initial = productBrandInitial(productBrand);
  if (id === 'signin') {
    return (
      <div className="mobile-content ready-app mock-screen">
        <MobileBrand productBrand={productBrand} />
        <div className="ready-hero"><small>WELCOME</small><h3>Sign in to your workspace.</h3><p>Use the member account from the brief.</p></div>
        <div className="mock-form"><span>Email</span><i /><span>Password</span><i /><b>Continue</b></div>
      </div>
    );
  }
  if (id === 'items') {
    return (
      <div className="mobile-content ready-app mock-screen">
        <MobileBrand productBrand={productBrand} />
        <h4>Your items</h4>
        {['Project overview', 'Shared collection', 'Launch notes'].map((item) => (
          <div className="nova-item" key={item}><span>▤</span><div><b>{item}</b><small>Updated today</small></div></div>
        ))}
        <div className="mobile-bottom">{['Home', 'Explore', 'Saved', 'Profile'].map((item) => <span key={item}>{item}</span>)}</div>
      </div>
    );
  }
  if (id === 'profile') {
    return (
      <div className="mobile-content ready-app mock-screen">
        <MobileBrand productBrand={productBrand} />
        <div className="profile-avatar">{initial}</div>
        <h3>Your workspace</h3>
        <p>Demo member</p>
        <div className="skeleton-box">Personal workspace<br /><small>Ready for review</small></div>
      </div>
    );
  }
  return (
    <div className="mobile-content ready-app mock-screen">
      <MobileBrand productBrand={productBrand} />
      <div className="ready-hero"><small>YOUR WORKSPACE</small><h3>Everything, in one place.</h3><p>Pick up where you left off.</p><b className="mock-cta">Create new</b></div>
      <div className="mobile-grid"><div className="tile">▣<b>My items</b><small>3 items</small></div><div className="tile">◇<b>Saved</b><small>1 item</small></div></div>
    </div>
  );
}

export function MockupReview({ productBrand = 'Project', onClose, onApprove, onReject }) {
  const [index, setIndex] = useState(0);
  const mockup = mockups[index];
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'ArrowRight') setIndex((value) => Math.min(mockups.length - 1, value + 1));
      if (event.key === 'ArrowLeft') setIndex((value) => Math.max(0, value - 1));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="mockup-review" role="dialog" aria-modal="true" aria-label="Review mockups">
      <div className="mockup-bar"><b>UI/UX mockups</b><span>{mockup.name}</span><button type="button" onClick={onClose} aria-label="Close mockups">×</button></div>
      <div className="mockup-stage">
        <button type="button" className="mockup-nav" aria-label="Previous mockup" disabled={index === 0} onClick={() => setIndex(index - 1)}>‹</button>
        <MobileFrame><MockScreen id={mockup.id} productBrand={productBrand} /></MobileFrame>
        <button type="button" className="mockup-nav" aria-label="Next mockup" disabled={index === mockups.length - 1} onClick={() => setIndex(index + 1)}>›</button>
      </div>
      <div className="mockup-dots">{mockups.map((item, itemIndex) => <button type="button" key={item.id} aria-label={item.name} aria-current={itemIndex === index ? 'true' : undefined} onClick={() => setIndex(itemIndex)} />)}</div>
      <GateActions onApprove={onApprove} onReject={onReject} />
    </div>
  );
}
