import Link from "next/link";

export default function Landing() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-24">
      <h1 className="text-4xl font-bold tracking-tight">Reliability Engineer</h1>
      <p className="mt-4 text-lg text-[var(--muted)]">
        Discover your app&apos;s workflows, load-test them with up to 1,000 virtual users, get
        evidence-backed diagnoses, and review independently verified fixes as pull requests.
      </p>
      <ul className="mt-6 list-disc space-y-1 pl-6 text-[var(--muted)]">
        <li>We only test hosts you prove you control.</li>
        <li>Coverage is reported as discovered vs. tested workflows — never as a guarantee.</li>
        <li>Fixes are proposals: verified in a sandbox, opened as PRs, merged only by you.</li>
      </ul>
      <Link
        href="/login"
        className="mt-10 inline-block rounded-md bg-[var(--accent)] px-5 py-2.5 font-medium text-white"
      >
        Sign in
      </Link>
      <p className="mt-6 text-sm text-[var(--muted)]">
        <Link className="underline" href="/pricing">Plans</Link> · <Link className="underline" href="/legal">Terms &amp; policies</Link>
      </p>
    </main>
  );
}
