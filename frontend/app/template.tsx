// A template (unlike the layout) mounts again on every navigation, so each view enters with the same short rise.
export default function Template({ children }: { children: React.ReactNode }) {
  return <div className="enter">{children}</div>;
}
