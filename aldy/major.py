# 786

# Aldy source: major.py
#   This file is subject to the terms and conditions defined in
#   file 'LICENSE', which is part of this source code package.


from typing import List, Dict, Tuple, Set, Any

import collections
import itertools
import functools
import multiprocessing
import copy

from . import lpinterface
from .common import *
from .cn import CNSolution, MAX_CN
from .gene import Allele, Mutation, Gene, CNConfig
from .sam import Sample, Coverage


class SolvedAllele(collections.namedtuple('SolvedAllele', ['major', 'minor', 'added', 'missing'])):
   """
   Describes a candidate star-allele configuration.
   Immutable class.

   Attributes:
      major (str):
         Major star-allele identifier.
      minor (str, optional):
         Minor star-allele identifier. Can be None.
      added (list[:obj:`aldy.gene.Mutation`]):
         List of mutations that are added to this copy of a major/minor star-allele
         (e.g. these mutations are not present in the database defition of allele).
      missing (list[:obj:`aldy.gene.Mutation`]):
         List of mutations that are ommited from this copy of a major/minor star-allele
         (e.g. these mutations are present in the database defition of allele but not in the sample).
   
   Notes:
      Has custom printer (``__str__``).
   """


   def major_repr(self):
      return '*{}{}'.format(self.major,
         ''.join(' +' + str(m) for m in sorted(m for m in self.added if m.is_functional)))


   def __str__(self):
      return '*{}{}{}'.format(
         self.minor if self.minor else self.major,
         ''.join(' +' + str(m) for m in sorted(self.added, 
                                               key=lambda m: (-m.is_functional, m.pos, m.op))),
         ''.join(' -' + str(m) for m in sorted(self.missing)))


class MajorSolution(collections.namedtuple('MajorSolution', ['score', 'solution', 'cn_solution'])):
   """
   Describes a potential (possibly optimal) major star-allele configuration.
   Immutable class.

   Attributes:
      score (float):
         ILP model error score (0 for user-provided solutions).
      solution (dict[:obj:`SolvedAllele`, int]):
         Dictionary of major star-alleles where each major star-allele is 
         associated with its copy number 
         (e.g. `{1: 2}` means that we have two copies of *1).
      cn_solution (:obj:`aldy.cn.CNSolution`):
         Associated copy-number solution used for calculating the major 
         star-alleles.
   
   Notes:
      Has custom printer (``__str__``).
   """
   
   def __str__(self):
      return f'MajorSol[{self.score:.2f}; ' + \
              'sol=({}); '.format(', '.join(f'{v}x{s}' for s, v in self.solution.items())) + \
             f'cn={self.cn_solution}'
      

def estimate_major(gene: Gene, 
                   sam: Sample, 
                   cn_solution: CNSolution, 
                   solver: str) -> List[MajorSolution]:
   """
   list[:obj:`MajorSolution`]: Detect the major star-alleles in the sample.

   Args:
      gene (:obj:`aldy.gene.Gene`): 
         A gene instance.
      sam (:obj:`aldy.sam.Sample`): 
         Read alignment data.
      cn_solution (:obj:`aldy.cn.CNSolution`): 
         Copy-number solution to be used for major star-allele calling.
      solver (str): 
         ILP solver to use. Check :obj:`aldy.lpinterface` for available solvers.
   """

   log.debug(f'>> sample = {sam.coverage._dump()}')

   log.debug('Solving major alleles for cn={}', cn_solution)
   
   # Case of two deletions
   if len(cn_solution.solution) == 0: 
      del_allele = next(a for a, cn in gene.cn_configs.items() 
                        if cn.kind == CNConfig.CNConfigType.DELETION)
      sol = MajorSolution(score=0, solution={del_allele: 2}, cn_solution=cn_solution, novel={})
      return [sol]
   
   alleles, coverage = _filter_alleles(gene, sam, cn_solution)
   # Check if some CN solution has no matching allele
   if set(cn_solution.solution) - set(a.cn_config for a in alleles.values()):
      results = [MajorSolution(score=float('inf'), solution=[], cn_solution=cn_solution, novel={})]
   else:
      results = solve_major_model(gene, alleles, coverage, cn_solution, solver)
   # TODO: re-implement phasing step from Aldy 1.4   
   # TODO: Check for novel functional mutations and do something with them
   # novel_functional_mutations = _get_novel_mutations(gene, coverage, cn_solution)

   log.debug(f'>> major_sol = {results.__repr__()}')
   return results


def solve_major_model(gene: Gene,
                      allele_dict: Dict[str, Allele], 
                      coverage: Coverage, 
                      cn_solution: CNSolution, 
                      solver: str) -> List[MajorSolution]:
   """
   list[:obj:`MajorSolution`]: Solves the major star-allele detection problem via integer linear programming.

   Args:
      gene (:obj:`aldy.gene.Gene`): 
         A gene instance.
      allele_dict (dict[str, :obj:`aldy.gene.Allele`]):
         Dictionary of candidate major star-alleles. 
      coverage (:obj:`aldy.coverage.Coverage`):
         Sample coverage used to find out the coverage of each major mutation
      cn_solution (:obj:`aldy.cn.CNSolution`):
         Copy-number solution to be used for detecting major star-alleles (check :obj:`aldy.cn.CNSolution`).
      solver (str): 
         ILP solver to use. Check :obj:`aldy.lpinterface` for available solvers.

   Notes:
      Please see Aldy paper (section Methods/Major star-allele identification) for the model explanation.
      Given:
      - a list of major alleles :math:`P` with
         - a binary variable :math:`p_i` indicating whether this major allele is the part of the solution
      - a vector of observed coverage :math:`\mathbf{sam}` for each mutation,
      - function :math:`\mathrm{cn}` that returns the copy number of the location that harbors some mutation,
      - variables :math:`M_{i,m}` that indicate will the mutation :math:`m` belong to the major allele :math:`p_i`
        (to account for the novel major alleles)
      solve:
      - :math:$$\min \sum_{m} 
         \left| \mathbf{sam}[m] - \sum_{i \in P} \frac{\mathbf{sam}[m]}{\mathrm{cn}(m)} p_i M_{i,m} \right| + 
         P \sum_{m \text{ not in the definition of } p_i} M_{i, m}$$,
      where:
      - :math:`P` is the novel mutation penalty
      subject to:
      - :math:`M_{i,m}` is always 1 if :math:`m` is in the definition of :math:`p_i`, 
      - copy numbers of selected major alleles match the provided copy-number solution, and
      - :math:`\sum_{i} M{i,m} \geq 1` for each expressed functional mutation :math:`m`.
   """

   # Model parameters
   # Make sure that each novel mutation gets heavily penalized
   NOVEL_MUTATION_PENAL = 100000

   # Make sure that coverage defaults to 0 on empty values
   model = lpinterface.model('aldy_major_allele', solver)
   _print_candidates(allele_dict, coverage, cn_solution)
   
   # hack to silence type checker
   a: Any = 0

   # Create a binary variable for all possible allele copies
   alleles = {(a, 0): allele_dict[a] for a in allele_dict} 
   for (an, _), a in list(alleles.items()):
      max_cn = cn_solution.solution[a.cn_config]
      log.trace('Maximum CN for {}: {}', an, max_cn)
      for i in range(1, max_cn):
         alleles[an, i] = alleles[an, 0]
   A = {a: model.addVar(vtype='B', name='A_{}_{}'.format(*a)) for a in alleles}

   # Make sure that A[i+1] <= A[i] (to avoid equivalent solutions)
   for a, ai in alleles.keys(): 
      if ai > 0:
         log.trace('LP contraint: A_{}_{} <= A_{}_{}', a, ai, a, ai - 1)
         model.addConstr(A[a, ai] <= A[a, ai - 1])
   
   # Add an error variable to the ILP for any mutation
   error_vars = {m: model.addVar(lb=-model.INF, ub=model.INF, name='MA_{}_{}_{}'.format(m, *a))
                 for a in alleles
                 for m in alleles[a].func_muts}
   constraints = {e: 0 for e in error_vars}
   # Add a binary variable for any allele/novel mutation pair
   # TODO: do not add novel variables in impossible CN regions
   del_allele = gene.deletion_allele()
   M = {a: {m: model.addVar(vtype='B', name='EXTRA_{}_{}_{}'.format(m, *a))
               if a[0] != del_allele
               else 0 # deletion alleles should not be assigned any mutations
            for m in constraints
            if m not in alleles[a].func_muts} 
        for a in alleles}
   # Populate constraints
   for a in alleles:
      for m in alleles[a].func_muts:
         cov = max(1, coverage.total(m.pos)) / cn_solution.position_cn(m.pos) \
            if cn_solution.position_cn(m.pos) > 0 else 0
         constraints[m] += cov * A[a]
   # Add novel mutation constraints
   for a in M:
      for m in M[a]:
         if cn_solution.position_cn(m.pos) == 0:
            continue
         cov = max(1, coverage.total(m.pos)) / cn_solution.position_cn(m.pos) \
            if cn_solution.position_cn(m.pos) > 0 else 0
         constraints[m] += cov * A[a] * M[a][m]
   
   # Populate constraints of non-variations (i.e. matches with the reference genome)
   for m in list(constraints):
      if m.op[:3] == 'INS':
         continue
      
      ref_m = Mutation(m.pos, 'REF') # type: ignore
      if ref_m in constraints:
         ref_m = Mutation(m.pos, ref_m.op + '#') # type: ignore
      if ref_m not in constraints:
         constraints[ref_m] = 0
         error_vars[ref_m] = model.addVar(lb=-model.INF, ub=model.INF, name=str(ref_m))

      cov = max(1, coverage.total(m.pos)) / cn_solution.position_cn(m.pos) \
         if cn_solution.position_cn(m.pos) > 0 else 0
      for a in alleles:
         constraints[ref_m] += cov * A[a]

   # Each allele must express all of its functional mutations
   for m, expr in constraints.items():
      log.trace('LP contraint: {} == {} + err for {} with cn={}', coverage[m], expr, m, cn_solution.position_cn(m.pos))
      model.addConstr(expr + error_vars[m] == coverage[m])

   # Each allele must express all of its functional mutations
   for cnf, cnt in cn_solution.solution.items():
      expr = sum(A[a] for a in A if alleles[a].cn_config == cnf)
      log.trace('LP contraint: {} == {} for {}', cnt, expr, cnf)
      model.addConstr(expr == cnt)

   # Each allele must express all of its functional mutations
   func_muts = (m for a in alleles for m in alleles[a].func_muts if coverage[m] > 0)
   for m in func_muts:
      expr = model.quicksum(A[a] for a in alleles if m in alleles[a].func_muts)
      expr += model.quicksum(A[a] * M[a][m] for a in alleles if m not in alleles[a].func_muts)
      log.trace('LP contraint: {} >= 1 for {}', expr, m)
      model.addConstr(expr >= 1)

   # Set objective: minimize the absolute sum of errors   
   objective = \
      model.abssum(e for e in error_vars.values()) + \
      NOVEL_MUTATION_PENAL * model.quicksum(M[a][m] for a in M for m in M[a])
   log.trace('LP objective: {}', objective)

   # Solve the ILP
   try:
      status, opt, solutions = model.solveAll(objective, 
         {**{(k,  ): v for k, v in A.items()}, # wrap (k) to ensure that tuples can be compared
          **{(a, m): M[a][m] for a in M for m in M[a] if a[0] != del_allele}})
      log.debug('Major Solver status: {}, opt: {}', status, opt)
   except lpinterface.NoSolutionsError:
      return [MajorSolution(score=float('inf'), solution=[], cn_solution=cn_solution, novel={})]

   result = []
   for sol in solutions:
      alleles = {} # dict of allele IDs -> novel mutations
      for s in sol: # handle 2-tuples properly (2-tuples have novel alleles)
         if len(s) == 2:
            alleles[s[0]].append(s[1])
         else:
            alleles[s[0]] = [] # li
      solution = collections.Counter(SolvedAllele(major=a, 
                                                  minor=None, 
                                                  added=tuple(mut), 
                                                  missing=tuple()) 
                                     for (a, _), mut in alleles.items())
      sol = MajorSolution(score=opt, 
                          solution=solution, 
                          cn_solution=cn_solution)
      log.debug('Major solution: {}'.format(sol))
      result.append(sol)
   
   return result


def _filter_alleles(gene: Gene, 
                    sam: Sample, 
                    cn_solution: CNSolution) -> Tuple[Dict[str, Allele], Coverage]:
   """
   tuple[dict[str, :obj:`aldy.gene.Allele`], :obj:`aldy.coverage.Coverage`]: Filters out 
   all low-quality mutations and impossible alleles. Returns an allele dictionary describing 
   feasible alleles and high-confidence variants.
   """
   
   def filter_fns(mut, cov, total, thres):
      return Coverage.basic_filter(mut, cov, total, thres / MAX_CN) and \
             Coverage.cn_filter(mut, cov, total, thres, cn_solution)
   cov = sam.coverage.filtered(filter_fns)
   alleles = copy.deepcopy(gene.alleles)
   for an, a in sorted(gene.alleles.items()):
      if a.cn_config not in cn_solution.solution:
         del alleles[an]
      elif any(cov[m] <= 0 for m in a.func_muts):
         s = ('{} in {}'.format(m, gene.region_at(m.pos))
              for m in a.func_muts
              if cov[m] <= 0)
         log.trace('Removing {} because of {}', an, ' and '.join(s))
         del alleles[an]
   
   return alleles, cov


def _get_novel_mutations(gene: Gene, 
                         coverage: Coverage, 
                         cn_solution: CNSolution) -> Set[Mutation]:
   """
   set[:obj:`aldy.gene.Mutation`]: Calculates the set of expressed major functional 
   mutations that are not present in the database.

   TODO: integrate this into the model.
   """

   # Require AT LEAST 80% coverage per copy for a nover mutation
   MIN_COVERAGE_PER_COPY = 80.0

   result = set()
   for pos, muts in coverage._coverage.items():
      for op, cov in muts.items():
         if op == '_' or (pos, op) in gene.mutations:
            continue
         try:
            _, region = gene.region_at(pos)
         except KeyError:
            continue
         # TODO: handle non-unique regions as well (remapping)
         if region not in gene.unique_regions:
            continue
         cn = cn_solution.position_cn(pos)
         if cn == 0 or coverage.percentage(Mutation(pos, op)) < MIN_COVERAGE_PER_COPY / cn: # type: ignore
            continue
         if gene.check_functional(Mutation(pos, op)): # type: ignore
            log.debug('Novel mutation: {} {} {} ({} or {}%)', gene.region_at(pos), pos, op, cov, coverage.percentage(Mutation(pos, op))) # type: ignore
            result.add(Mutation(pos, op, is_functional=True)) # type: ignore
   return result


def _print_candidates(alleles: Dict[str, Allele], 
                      coverage: Coverage, 
                      cn_solution: CNSolution) -> None:
   """
   Pretty-prints the list of allele candidates and their functional mutations.
   """
   log.debug('Possible candidates:')
   for a in sorted(alleles, key=allele_sort_key):
      log.debug('  *{} (cn=*{})', a, alleles[a].cn_config)
      for m in sorted(alleles[a].func_muts, key=lambda m: m.pos):
         log.debug('    {} {:4} ({:.1f} copies) {} {}',
            #coverage.region_at(m.pos),
            m, coverage[m], 
            coverage[m] / (coverage.total(m.pos) / cn_solution.position_cn(m.pos)),
            'F', m.aux.get('old', ''))