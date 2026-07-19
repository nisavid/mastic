import unittest

from mastic.domain.admission import (
    AdmissionDecision,
    FitAssessment,
    FitClass,
    PressureAction,
    PressureLevel,
    PressurePolicy,
    RunningService,
)


class AdmissionPolicyTests(unittest.TestCase):
    def test_likely_fit_starts_and_borderline_requires_confirmation(self) -> None:
        self.assertEqual(
            FitAssessment(FitClass.LIKELY, 40, 64, ("measured weights",)).decision,
            AdmissionDecision.START,
        )
        self.assertEqual(
            FitAssessment(FitClass.BORDERLINE, 58, 64, ("derived KV",)).decision,
            AdmissionDecision.CONFIRM,
        )
        self.assertEqual(
            FitAssessment(FitClass.UNKNOWN, None, 64, ("missing config",)).decision,
            AdmissionDecision.CONFIRM,
        )

    def test_no_fit_requires_named_transition_sequence(self) -> None:
        fit = FitAssessment(FitClass.NO_FIT, 80, 64, ("exact model bytes",))
        self.assertEqual(fit.decision, AdmissionDecision.TRANSITION_SEQUENCE)
        with self.assertRaisesRegex(ValueError, "named transition"):
            fit.approve_transition(())
        self.assertEqual(fit.approve_transition(("stop:chat",)), ("stop:chat",))

    def test_critical_pressure_sheds_then_stops_lru_idle_unpinned(self) -> None:
        services = (
            RunningService("coding", pinned=True, busy=False, last_used_ns=1),
            RunningService("chat", pinned=False, busy=False, last_used_ns=2),
            RunningService("review", pinned=False, busy=False, last_used_ns=3),
            RunningService("vision", pinned=False, busy=True, last_used_ns=0),
        )

        result = PressurePolicy().evaluate(PressureLevel.CRITICAL, services)

        self.assertEqual(result.actions[0], PressureAction.SHED_NEW_WORK)
        self.assertEqual(result.stop_services, ("chat", "review"))
        self.assertNotIn("coding", result.stop_services)

    def test_only_pinned_or_busy_services_produces_explicit_stop_sequence(
        self,
    ) -> None:
        services = (
            RunningService("coding", pinned=True, busy=False, last_used_ns=1),
            RunningService("vision", pinned=False, busy=True, last_used_ns=2),
        )

        result = PressurePolicy().evaluate(PressureLevel.CRITICAL, services)

        self.assertEqual(result.stop_services, ())
        self.assertIn(PressureAction.PRESENT_STOP_SEQUENCE, result.actions)
        self.assertEqual(result.operator_stop_sequence, ("vision", "coding"))

    def test_critical_pressure_without_services_only_sheds_new_work(self) -> None:
        result = PressurePolicy().evaluate(PressureLevel.CRITICAL, ())

        self.assertEqual(result.actions, (PressureAction.SHED_NEW_WORK,))
        self.assertEqual(result.stop_services, ())
        self.assertEqual(result.operator_stop_sequence, ())


if __name__ == "__main__":
    unittest.main()
