import { motion, useReducedMotion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";

type OperationalLoadingProps = {
  progress?: number;
  autoAdvance?: boolean;
  className?: string;
  title?: string;
  description?: string;
  badgeText?: string;
};

const clamp = (value: number, min: number, max: number) =>
  Math.min(Math.max(value, min), max);

export function OperationalLoading({
  progress,
  autoAdvance = true,
  className = "",
  title = "Gerando resposta operacional...",
  description = "Consultando a base e montando a melhor resposta para voce.",
  badgeText = "Pode levar alguns segundos",
}: OperationalLoadingProps) {
  const prefersReducedMotion = useReducedMotion();
  const [internalProgress, setInternalProgress] = useState(18);

  useEffect(() => {
    if (typeof progress === "number") {
      setInternalProgress(clamp(progress, 0, 100));
      return;
    }

    if (!autoAdvance) {
      return;
    }

    const steps = [24, 31, 39, 47, 55, 62, 68, 73, 78, 82, 86, 89, 92, 94];
    let stepIndex = 0;

    const interval = window.setInterval(() => {
      setInternalProgress((current) => {
        if (current >= 94) {
          return current;
        }

        const next = steps[Math.min(stepIndex, steps.length - 1)];
        stepIndex += 1;
        return Math.max(current, next);
      });
    }, 520);

    return () => window.clearInterval(interval);
  }, [autoAdvance, progress]);

  const value = useMemo(
    () => clamp(typeof progress === "number" ? progress : internalProgress, 0, 100),
    [internalProgress, progress],
  );

  const orbitTransition = prefersReducedMotion
    ? { duration: 0 }
    : { duration: 2.8, repeat: Infinity, ease: "linear" as const };

  const glowTransition = prefersReducedMotion
    ? { duration: 0 }
    : { duration: 2.2, repeat: Infinity, repeatType: "mirror" as const, ease: "easeInOut" as const };

  const pulseTransition = prefersReducedMotion
    ? { duration: 0 }
    : { duration: 1.8, repeat: Infinity, repeatType: "mirror" as const, ease: "easeInOut" as const };

  return (
    <section
      aria-live="polite"
      aria-busy="true"
      className={[
        "relative w-full max-w-xl overflow-hidden rounded-[18px] border border-blue-400/15",
        "bg-[radial-gradient(circle_at_18%_12%,rgba(96,165,250,0.16),transparent_34%),linear-gradient(180deg,rgba(12,18,36,0.94),rgba(8,13,28,0.84))]",
        "p-4 shadow-[0_18px_58px_rgba(0,0,0,0.34),0_0_34px_rgba(37,99,235,0.14)] backdrop-blur-xl",
        "before:pointer-events-none before:absolute before:inset-0 before:rounded-[18px]",
        "before:border before:border-white/[0.045] before:content-['']",
        className,
      ].join(" ")}
    >
      <motion.div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-12 top-0 h-px bg-gradient-to-r from-transparent via-blue-400/80 to-transparent"
        animate={prefersReducedMotion ? undefined : { opacity: [0.35, 0.9, 0.35] }}
        transition={glowTransition}
      />

      <div className="relative z-10">
        <h3 className="mb-4 text-sm font-semibold leading-snug text-blue-100 sm:text-[15px]">
          {title}
        </h3>

        <div className="grid grid-cols-[46px_minmax(0,1fr)] items-center gap-4 sm:grid-cols-[52px_minmax(0,1fr)]">
          <div className="relative flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-full bg-slate-950/20 sm:h-[52px] sm:w-[52px]">
            <motion.div
              aria-hidden="true"
              className="absolute inset-0 rounded-full bg-blue-400/10 blur-xl"
              animate={prefersReducedMotion ? undefined : { scale: [0.92, 1.08, 0.92], opacity: [0.35, 0.76, 0.35] }}
              transition={glowTransition}
            />

            <div className="absolute inset-0 rounded-full border border-white/10 bg-white/[0.025]" />

            <svg
              aria-hidden="true"
              viewBox="0 0 100 100"
              className="relative h-[46px] w-[46px] -rotate-90 sm:h-[52px] sm:w-[52px]"
            >
              <circle
                cx="50"
                cy="50"
                r="34"
                className="fill-none stroke-blue-400/10"
                strokeWidth="9"
              />
              <motion.circle
                cx="50"
                cy="50"
                r="34"
                className="fill-none stroke-blue-400"
                strokeLinecap="round"
                strokeWidth="9"
                strokeDasharray="126 214"
                animate={prefersReducedMotion ? undefined : { rotate: 360 }}
                transition={orbitTransition}
                style={{
                  filter: "drop-shadow(0 0 10px rgba(96, 165, 250, 0.62))",
                  transformOrigin: "50% 50%",
                }}
              />
            </svg>
          </div>

          <div className="min-w-0 flex-1">
            <p className="max-w-md text-sm leading-6 text-indigo-100/85 sm:text-[15px]">
              {description}
            </p>

            <div className="mt-4 grid grid-cols-[minmax(0,1fr)_42px] items-center gap-3">
              <div
                className="relative h-2.5 flex-1 overflow-hidden rounded-full bg-slate-700/45 shadow-[inset_0_1px_2px_rgba(0,0,0,0.34)]"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(value)}
                aria-label="Progresso da resposta"
              >
                <motion.div
                  aria-hidden="true"
                  className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-[#5b6cff] via-[#4f8dff] to-sky-400"
                  initial={false}
                  animate={{ width: `${value}%` }}
                  transition={{
                    duration: prefersReducedMotion ? 0 : 0.75,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                  style={{
                    boxShadow:
                      "0 0 18px rgba(79, 141, 255, 0.48), 0 0 34px rgba(56, 189, 248, 0.16)",
                  }}
                />
                <motion.div
                  aria-hidden="true"
                  className="absolute inset-y-0 w-20 rounded-full bg-gradient-to-r from-white/0 via-white/25 to-white/0"
                  animate={prefersReducedMotion ? undefined : { x: ["-30%", "420%"] }}
                  transition={{
                    duration: 2.4,
                    repeat: Infinity,
                    ease: "linear",
                  }}
                />
              </div>

              <motion.span
                className="text-right text-sm font-medium tabular-nums text-blue-100"
                initial={false}
                animate={{ opacity: [0.82, 1, 0.82] }}
                transition={pulseTransition}
              >
                {Math.round(value)}%
              </motion.span>
            </div>

            <div className="mt-4 flex justify-center">
              <span
                className={[
                  "inline-flex items-center rounded-full border border-slate-300/15 px-3 py-1.5",
                  "bg-slate-950/30 text-xs font-medium text-slate-400",
                  "shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]",
                ].join(" ")}
              >
                {badgeText}
              </span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export default OperationalLoading;
