import { LazyMotion, MotionConfig, domAnimation, m } from "framer-motion";
import { type ReactNode, useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

/** Motion tokens: same values as --adp-dur-* / --adp-ease (DESIGN.md §2.4), in seconds. */
export const DUR = { fast: 0.12, base: 0.2, slow: 0.32 } as const;
export const EASE = [0.2, 0, 0, 1] as const;

/** Live `prefers-reduced-motion: reduce`. Without matchMedia (SSR, old test env) we do not animate. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => typeof matchMedia !== "function" || matchMedia(QUERY).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(QUERY);
    const on = () => setReduced(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

/** Single source of truth for Framer: our hook decides, Framer never reads the media query itself. */
export function MotionProvider({ children }: { children: ReactNode }) {
  const reduced = useReducedMotion();
  return (
    <LazyMotion features={domAnimation}>
      <MotionConfig reducedMotion={reduced ? "always" : "never"} transition={{ duration: reduced ? 0 : DUR.base, ease: EASE }}>
        {children}
      </MotionConfig>
    </LazyMotion>
  );
}

/** Short fade + 6 px rise on mount (opacity/transform only). Reduced motion: rendered in its final state, no transform. */
export function Reveal({ children, className, rise = 6 }: { children: ReactNode; className?: string; rise?: number }) {
  const reduced = useReducedMotion();
  return (
    <m.div className={className} initial={reduced ? false : { opacity: 0, y: rise }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? 0 : DUR.base, ease: EASE }}>
      {children}
    </m.div>
  );
}

/** Quality-bar fill: left-to-right reveal with clip-path (keeps the hatching undistorted, no layout). Value stays readable without it. */
export function Fill({ percent }: { percent: number }) {
  const reduced = useReducedMotion();
  return (
    <m.div
      className="fill"
      style={{ width: `${percent}%` }}
      initial={reduced ? false : { clipPath: "inset(0 100% 0 0)" }}
      animate={{ clipPath: "inset(0 0% 0 0)" }}
      transition={{ duration: reduced ? 0 : DUR.slow, ease: EASE }}
    />
  );
}
